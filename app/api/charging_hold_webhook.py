"""
Webhook підтвердження hold-інвойсу Моделі B (Промпт 3c-ii, грн через
Monobank hold/finalize/cancel).

    POST /webhook/charging-hold/{operator_id}
      -> дістаємо invoiceId з тіла
      -> шукаємо резервацію (charging_reservations, НЕ operator_payments —
         окремий грошовий потік, розрізнення на рівні РОУТИНГУ, той самий
         аргумент, що для wallet_topups/operator_payments) з цим invoice_id
         САМЕ цього operator_id
      -> питаємо банк GET /api/merchant/invoice/status токеном оператора
      -> віримо ЛИШЕ відповіді банку (той самий принцип 2a, app/api/
         operator_webhook.py) — тіло webhook саме по собі нічого не доводить

Якщо банк підтверджує 'hold' і локальний статус 'awaiting_hold' —
мʼютексний перехід у 'pending' + RemoteStart, У ТОМУ САМОМУ FastAPI
app/uvicorn-процесі, що й `/ocpp` WS-роут (app/api/ocpp_ws.py). Це
навмисно: RemoteStart звертається до `_active_charge_points`
(app/api/ocpp_ws.py) — модульного словника в памʼяті ЖИВОГО процесу.
Виклик з ОКРЕМОГО процесу (як reconcile_charging_reservations.py,
docker compose exec) тут НЕМОЖЛИВИЙ за конструкцією — той самий клас бага,
що "3c-i незапускний на проді" (див. docs/plan-3c-ii.md, «Інваріант
процесу»). Тому цей вебхук — ЄДИНЕ місце (крім самого адмінського запуску
резервації), звідки для Моделі B взагалі можна безпечно смикнути
RemoteStart.

Герметизація (docs/plan-3c-ii.md, розділ 3, правка 5 рев'ю): між
мʼютексним переходом і підтвердженим RemoteStart може статись БУДЬ-ЩО (не
лише ChargePointNotConnected — мережевий збій, таймаут, помилка
ocpp-бібліотеки, asyncio.CancelledError) — try/except BaseException,
компенсація під asyncio.shield(), той самий ідіом, що
feature/ocpp-hold-hardening (app/services/ocpp_charging.py::
start_charging_session). Компенсація — НЕГАЙНИЙ cancel_invoice() (НЕ
release_reservation_hold — та функція fail-closed відмовляє uah-рядку
фільтром payment_method='kwh'; НЕ очікування --pending-minutes звірки —
тримати гроші водія заблокованими зайві хвилини без потреби невиправдано,
коли RemoteStart точно не пройшов).

Невідомий invoiceId -> тихий 200 без подробиць — той самий анти-оракул
принцип, що й у app/api/operator_webhook.py / app/api/wallet_webhook.py:
відповідь однакова для «інвойсу не існує», «інвойс чужого оператора» і
«усе гаразд».
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Request, Response, status

from app.api.ocpp_ws import ChargePointNotConnected, remote_start_transaction
from app.core.crypto import EncryptionKeyMissing, decrypt_secret
from app.database import operators_repo as repo
from app.services.monobank_acquiring import MonobankError, cancel_invoice, get_invoice_status

logger = logging.getLogger(__name__)

charging_hold_webhook_router = APIRouter()

_QUIET_OK = Response(status_code=status.HTTP_200_OK)


async def _cancel_after_failed_remote_start(operator_id: int, reservation_id: int,
                                            operator_token: str, invoice_id: str, reason: str) -> None:
    """
    Компенсація: скасовує hold у банку НЕГАЙНО (не чекаючи звірки) і
    синхронізує локальний статус на 'cancelled' (той самий статус, що
    кВт·год-модель використовує для "RemoteStart не пройшов одразу після
    резервації" — start_charging_session/release_reservation_hold(...,
    "cancelled"); 'expired' лишається за звіркою для протухлого 'pending').

    Збій самого cancel_invoke() ЛОГУЄТЬСЯ, але не перевикидається з ЦІЄЇ
    функції (виклик іде з внутрішнього try/except нижче, що вже все одно
    ловить BaseException) — якщо банк недоступний саме зараз, рядок
    лишається 'pending', і крок 2 звірки (docs/plan-3c-ii.md розділ 5)
    сам перепитає банк і довершить cancel пізніше.
    """
    try:
        await cancel_invoice(operator_token, invoice_id)
        await repo.record_uah_settlement(operator_id, reservation_id, "cancelled", 0)
        logger.info("↩️ Webhook hold-резервацій: hold резервації #%s скасовано (%s)",
                    reservation_id, reason)
    except MonobankError as e:
        logger.error(
            "🔥 Webhook hold-резервацій: не вдалося скасувати hold резервації #%s (%s) — "
            "лишається 'pending', довершить reconcile_charging_reservations.py: %s",
            reservation_id, reason, e,
        )


async def _remote_start_with_compensation(operator_id: int, reservation: dict, operator_token: str) -> None:
    """
    RemoteStart ПІСЛЯ мʼютексного переходу awaiting_hold -> pending.
    try/except BaseException (не Exception — asyncio.CancelledError у
    Python 3.8+ від BaseException), компенсація під asyncio.shield() —
    той самий ідіом, що app/services/ocpp_charging.py::
    start_charging_session() (feature/ocpp-hold-hardening).
    """
    reservation_id = reservation["id"]
    try:
        station = await repo.get_station(operator_id, reservation["station_id"])
        if station is None or station.get("ocpp_charge_point_id") is None:
            await _cancel_after_failed_remote_start(
                operator_id, reservation_id, operator_token, reservation["invoice_id"], "not_ocpp",
            )
            return

        cp_id = station["ocpp_charge_point_id"]
        try:
            accepted = await remote_start_transaction(operator_id, cp_id, id_tag=reservation["id_tag"])
        except ChargePointNotConnected:
            await _cancel_after_failed_remote_start(
                operator_id, reservation_id, operator_token, reservation["invoice_id"], "not_connected",
            )
            return

        if not accepted:
            await _cancel_after_failed_remote_start(
                operator_id, reservation_id, operator_token, reservation["invoice_id"], "rejected",
            )
            return

        logger.info("🔒 Webhook hold-резервацій: резервація #%s (оператор %s) — RemoteStart прийнято",
                    reservation_id, operator_id)
    except BaseException:
        logger.exception(
            "🔥 Webhook hold-резервацій: неочікуваний збій між підтвердженням hold і RemoteStart — "
            "скасовую hold резервації #%s (оператор %s)", reservation_id, operator_id,
        )
        try:
            await asyncio.shield(_cancel_after_failed_remote_start(
                operator_id, reservation_id, operator_token, reservation["invoice_id"], "unexpected_error",
            ))
        except BaseException:
            logger.error(
                "🔥 Webhook hold-резервацій: не дочекались підтвердження скасування hold "
                "резервації #%s (оператор %s) — cancel міг упасти, а міг і дожити у фоні під "
                "shield; фінальна сітка — крон reconcile_charging_reservations.py",
                reservation_id, operator_id,
            )
        raise


@charging_hold_webhook_router.post("/webhook/charging-hold/{operator_id}")
async def charging_hold_webhook(operator_id: int, request: Request):
    try:
        raw_body = await request.body()
        payload = json.loads(raw_body) if raw_body else {}
    except Exception:
        logger.warning("Webhook hold-резервацій оператора %s: тіло не є валідним JSON", operator_id)
        return _QUIET_OK

    invoice_id = (payload.get("invoiceId") or "").strip() if isinstance(payload, dict) else ""
    if not invoice_id:
        logger.warning("Webhook hold-резервацій оператора %s: у тілі немає invoiceId", operator_id)
        return _QUIET_OK

    # 1. Резервація має належати саме тому оператору, що в URL.
    reservation = await repo.get_reservation_by_invoice_id(operator_id, invoice_id)
    if reservation is None:
        logger.warning(
            "Webhook hold-резервацій оператора %s: інвойс %s не знайдено серед "
            "резервацій цього оператора — ігноруємо", operator_id, invoice_id,
        )
        return _QUIET_OK

    # 2. Уже оброблена резервація повторно не чіпається.
    if reservation["status"] != "awaiting_hold":
        logger.info(
            "Webhook hold-резервацій оператора %s: резервація #%s уже в статусі '%s' — "
            "повтор проігноровано", operator_id, reservation["id"], reservation["status"],
        )
        return _QUIET_OK

    # 3. Токен оператора потрібен, щоб спитати банк.
    token_encrypted = await repo.get_operator_monobank_token_encrypted(operator_id)
    if not token_encrypted:
        logger.error(
            "Webhook hold-резервацій оператора %s: резервація #%s є, але еквайринг-токен "
            "оператора не збережений — підтвердити hold неможливо",
            operator_id, reservation["id"],
        )
        return _QUIET_OK

    try:
        operator_token = decrypt_secret(token_encrypted)
    except (EncryptionKeyMissing, ValueError) as e:
        logger.error("Webhook hold-резервацій оператора %s: не вдалося розшифрувати токен: %s",
                     operator_id, e)
        return _QUIET_OK

    # 4. Єдине джерело правди — відповідь банку, НЕ тіло webhook.
    try:
        invoice = await get_invoice_status(operator_token, invoice_id)
    except MonobankError as e:
        logger.error("Webhook hold-резервацій оператора %s: не вдалося перевірити інвойс %s: %s",
                     operator_id, invoice_id, e)
        return Response(status_code=status.HTTP_502_BAD_GATEWAY)

    bank_status = (invoice.get("status") or "").strip()
    if bank_status != "hold":
        logger.info(
            "Webhook hold-резервацій оператора %s: інвойс %s у стані банку '%s' (не 'hold') "
            "— нічого не робимо", operator_id, invoice_id, bank_status,
        )
        return _QUIET_OK

    # 5. Мʼютексний перехід — рівно один паралельний виклик (повторний
    # webhook, резервний тригер звірки) проведе RemoteStart.
    confirmed = await repo.mark_reservation_hold_confirmed(operator_id, reservation["id"])
    if not confirmed:
        logger.info(
            "Webhook hold-резервацій оператора %s: резервація #%s уже переведена в 'pending' "
            "паралельно", operator_id, reservation["id"],
        )
        return _QUIET_OK

    await _remote_start_with_compensation(operator_id, reservation, operator_token)
    return _QUIET_OK
