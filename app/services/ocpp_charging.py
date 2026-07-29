"""
Сервісне ядро "резерв + RemoteStart" / "RemoteStop" (Промпт 3c-i, бот-
адмінкоманди). Один код для CLI (start_charging_session.py) і бот-команд
(app/handlers/ocpp_admin.py) — раніше цю логіку мав лише скрипт, який
запускається ОКРЕМИМ процесом (docker compose exec) і тому НІКОЛИ не бачить
_active_charge_points живого uvicorn-процесу (де реально висять OCPP-
з'єднання станцій) — RemoteStart з нього завжди падав з
ChargePointNotConnected. Ці функції призначені для виклику IN-PROCESS (з
бот-хендлера, що працює в ТОМУ САМОМУ event loop, що й OCPP WS-роут), де
реєстр реально доступний — саме це і робить /ocpp_start/ocpp_stop
працездатними на проді.

Жодних print()/side-effect'ів під конкретний канал — результат
структурований (ChargingStartResult/ChargingStopResult), форматування під
консоль чи Telegram лишається виклику (start_charging_session.py /
app/handlers/ocpp_admin.py відповідно).
"""
import asyncio
import logging
import secrets
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from app.api.ocpp_ws import ChargePointNotConnected, remote_start_transaction, remote_stop_transaction
from app.core.crypto import EncryptionKeyMissing, decrypt_secret
from app.database import operators_repo as repo
from app.services.monobank_acquiring import MonobankError, cancel_invoice, create_invoice, finalize_invoice

logger = logging.getLogger(__name__)


@dataclass
class ChargingStartResult:
    """
    status:
      "ok"                    — резерв взято, RemoteStart прийнято станцією.
      "unknown_station"       — station_id не належить operator_id.
      "insufficient_balance"  — недостатньо kWh-балансу водія (hold не пройшов).
      "not_ocpp"               — станція не в режимі OCPP (немає ocpp_charge_point_id).
      "not_connected"          — станція зараз не підключена до ЦЬОГО процесу.
      "rejected"                — станція відповіла Rejected на RemoteStartTransaction.

    reservation_id/id_tag — None лише для unknown_station/insufficient_balance
    (резервацію тоді ще не було створено). Для решти негативних статусів
    резервація БУЛА створена, але вже звільнена (release_reservation_hold,
    "cancelled") — hold і release компенсують одне одного, нетто-вплив на
    баланс водія 0.
    """
    status: str
    reservation_id: Optional[int] = None
    id_tag: Optional[str] = None


@dataclass
class ChargingStopResult:
    """
    status: "ok" | "unknown_station" | "no_active_session" | "not_connected" | "rejected"
    transaction_id — None лише для unknown_station/no_active_session.
    """
    status: str
    transaction_id: Optional[int] = None


async def start_charging_session(operator_id: int, station_id: int, user_id: int,
                                 reserved_kwh: Decimal) -> ChargingStartResult:
    """
    Резерв kWh-балансу + RemoteStartTransaction. На КОЖНІЙ гілці відмови
    ПІСЛЯ того, як резервація вже реально створена (not_ocpp/not_connected/
    rejected) — release_reservation_hold(..., "cancelled"), щоб hold водія
    не завис без жодного шансу на зарядку. Баланс тут ніде не чіпається
    напряму — лише через create_charging_reservation()/
    release_reservation_hold(), обидві йдуть через update_user_balance().

    Викликач відповідає за валідацію reserved_kwh (Decimal > 0) ДО виклику
    — тут лише передбачається валідне значення (DB-обмеження reserved_kwh >
    0 лишається останнім рубежем).
    """
    reservation_id, id_tag, error = await repo.create_charging_reservation(
        operator_id, station_id, user_id, reserved_kwh,
    )
    if error is not None:
        return ChargingStartResult(status=error)

    # Від цього рядка й до кінця функції баланс водія вже списано в hold
    # (create_charging_reservation() вище відпрацював). Три await нижче —
    # get_station/remote_start_transaction — можуть впасти НЕ лише
    # ChargePointNotConnected (мережевий збій, таймаут, помилка ocpp-
    # бібліотеки, websockets.ConnectionClosed, asyncio.CancelledError при
    # рестарті контейнера). Будь-який вихід звідси без release лишає hold
    # застряглим і водій тихо втрачає кВт·год — тому весь блок нижче в
    # try/except BaseException (не Exception: CancelledError у Python 3.8+
    # успадковується від BaseException, а скасування задачі рівно тут —
    # цілком реальний сценарій).
    try:
        station = await repo.get_station(operator_id, station_id)
        if station is None or station.get("ocpp_charge_point_id") is None:
            await repo.release_reservation_hold(operator_id, reservation_id, "cancelled")
            return ChargingStartResult(status="not_ocpp", reservation_id=reservation_id, id_tag=id_tag)

        cp_id = station["ocpp_charge_point_id"]
        try:
            accepted = await remote_start_transaction(operator_id, cp_id, id_tag=id_tag)
        except ChargePointNotConnected:
            await repo.release_reservation_hold(operator_id, reservation_id, "cancelled")
            return ChargingStartResult(status="not_connected", reservation_id=reservation_id, id_tag=id_tag)

        if not accepted:
            await repo.release_reservation_hold(operator_id, reservation_id, "cancelled")
            return ChargingStartResult(status="rejected", reservation_id=reservation_id, id_tag=id_tag)

        return ChargingStartResult(status="ok", reservation_id=reservation_id, id_tag=id_tag)
    except BaseException:
        logger.exception(
            "🔥 Неочікуваний збій між hold і RemoteStart — звільняю резервацію #%s "
            "(operator_id=%s, station_id=%s, user_id=%s)",
            reservation_id, operator_id, station_id, user_id,
        )
        try:
            # shield: захищає САМЕ від ПОВТОРНОГО task.cancel() на цій же
            # задачі (напр. штатний shutdown, що скасовує все двічі) — тоді
            # плейн await тут теж кинув би CancelledError і release не
            # дочекався б підтвердження. При одиночному cancel плейн await
            # у except-блоці й без shield спокійно добігає (прапорець
            # скасування знімається після першого кидка) — перевірено на
            # Python 3.11 (той самий інтерпретатор, що в Dockerfile і CI).
            # shield лишається про запас на другий сценарій, а не тому, що
            # без нього release взагалі не виконався б.
            await asyncio.shield(repo.release_reservation_hold(operator_id, reservation_id, "cancelled"))
        except BaseException:
            # Ловиться рівно в сценарії повторного cancel: зовнішній await
            # на shield кидає CancelledError, а сама корутина release під
            # shield могла і впасти, і успішно дожити у фоні — звідси ми
            # цього напевно не знаємо. Первинну причину це логування
            # маскувати не повинно:
            logger.error(
                "🔥 Не дочекались підтвердження звільнення резервації #%s (operator_id=%s) — "
                "release міг упасти, а міг і дожити у фоні під shield; фінальна сітка — "
                "крон reconcile_charging_reservations.py",
                reservation_id, operator_id,
            )
        raise


async def stop_charging_session(operator_id: int, station_id: int) -> ChargingStopResult:
    """
    RemoteStopTransaction на активну OCPP-сесію станції. Жодного settle
    тут: фактичне списання спожитого й звільнення залишку резерву робить
    ВИКЛЮЧНО on_stop_transaction (app/api/ocpp_ws.py), коли станція сама
    відповість власним StopTransaction.req — ця функція лише посилає
    команду й повертає, чи станція її прийняла.
    """
    station = await repo.get_station(operator_id, station_id)
    if station is None or station.get("ocpp_charge_point_id") is None:
        return ChargingStopResult(status="unknown_station")

    session = await repo.get_active_ocpp_session(operator_id, station_id)
    if session is None:
        return ChargingStopResult(status="no_active_session")

    transaction_id = session["ocpp_transaction_id"]
    cp_id = station["ocpp_charge_point_id"]
    try:
        accepted = await remote_stop_transaction(operator_id, cp_id, transaction_id)
    except ChargePointNotConnected:
        return ChargingStopResult(status="not_connected", transaction_id=transaction_id)

    if not accepted:
        return ChargingStopResult(status="rejected", transaction_id=transaction_id)

    return ChargingStopResult(status="ok", transaction_id=transaction_id)


# ---------------------------------------------------------------------------
# Модель B — гривневий hold через Monobank (Промпт 3c-ii)
#
# На відміну від kWh-моделі вище, тут НЕМАЄ єдиної RemoteStart-у-відповідь-
# на-команду: оплата водієм асинхронна (відкриває pageUrl, платить карткою
# коли завгодно), тому старт зарезервованого hold і фактичний RemoteStart —
# ДВА окремі кроки рознесені в часі. Перший — start_charging_reservation_
# uah() нижче (створює інвойс + локальний 'awaiting_hold'-рядок). Другий —
# у вебхуку (app/api/charging_hold_webhook.py), коли банк підтвердить hold.
# ---------------------------------------------------------------------------

@dataclass
class ChargingStartUahResult:
    """
    status:
      "ok"                — інвойс створено, водій може платити за page_url.
      "unknown_station"   — station_id не належить operator_id.
      "not_ocpp"          — станція не в режимі OCPP.
      "no_monobank_token" — у оператора не збережено (чи не розшифровується)
                            токен еквайрингу — платити нікуди.
      "bank_error"        — банк не створив інвойс (недоступний/відмовив).

    reservation_id/id_tag/page_url — None, якщо статус не "ok".
    """
    status: str
    reservation_id: Optional[int] = None
    id_tag: Optional[str] = None
    page_url: Optional[str] = None


async def start_charging_reservation_uah(operator_id: int, station_id: int, hold_amount_uah,
                                         redirect_url: str, webhook_url: str,
                                         driver_contact: str = None) -> ChargingStartUahResult:
    """
    Створює hold-інвойс у банку (paymentType="hold") токеном ОПЕРАТОРА +
    локальну резервацію одразу у статусі 'awaiting_hold'
    (repo.create_charging_reservation_uah). RemoteStart тут НЕ
    відбувається — станція стартує лише коли банк підтвердить hold
    (вебхук, docs/plan-3c-ii.md розділ 3, варіант А).

    Станція перевіряється (існує, режим OCPP) ДО дзвінка в банк — дешевше
    не створювати інвойс узагалі, ніж створювати й одразу скасовувати.
    """
    station = await repo.get_station(operator_id, station_id)
    if station is None:
        return ChargingStartUahResult(status="unknown_station")
    if station.get("ocpp_charge_point_id") is None:
        return ChargingStartUahResult(status="not_ocpp")

    token_encrypted = await repo.get_operator_monobank_token_encrypted(operator_id)
    if not token_encrypted:
        return ChargingStartUahResult(status="no_monobank_token")
    try:
        operator_token = decrypt_secret(token_encrypted)
    except (EncryptionKeyMissing, ValueError) as e:
        logger.error("Модель B: не вдалося розшифрувати токен оператора %s: %s", operator_id, e)
        return ChargingStartUahResult(status="no_monobank_token")

    try:
        invoice = await create_invoice(
            operator_token,
            amount_uah=hold_amount_uah,
            reference=f"charging-hold-{station_id}-{secrets.token_hex(4)}",
            redirect_url=redirect_url,
            webhook_url=webhook_url,
            destination=f"Зарядка {station['name']}: гарантія {hold_amount_uah} грн, спишеться лише спожите",
            payment_type="hold",
        )
    except MonobankError as e:
        logger.error("Модель B: банк не створив hold-інвойс (оператор=%s станція=%s): %s",
                     operator_id, station_id, e)
        return ChargingStartUahResult(status="bank_error")

    reservation_id, id_tag, error = await repo.create_charging_reservation_uah(
        operator_id, station_id, hold_amount_uah, invoice["invoiceId"], driver_contact=driver_contact,
    )
    if error is not None:
        # Теоретично неможливо (станція вже перевірена вище), захист про
        # всяк випадок: інвойс у банку лишиться неоплаченим і сам протухне
        # за validity — гроші водія не в грі, поки він не заплатив.
        logger.error("Модель B: create_charging_reservation_uah повернув %s (оператор=%s станція=%s)",
                     error, operator_id, station_id)
        return ChargingStartUahResult(status=error)

    return ChargingStartUahResult(
        status="ok", reservation_id=reservation_id, id_tag=id_tag, page_url=invoice["pageUrl"],
    )


def compute_uah_settlement_amount(tariff_uah_start, tariff_uah_kwh, kwh, reserved_uah) -> Decimal:
    """
    Чиста функція (без I/O) — вартість фактично спожитого в грн, КАПОВАНА
    утриманою сумою (рішення Q4/варіант 1, docs/plan-3c-ii.md розділ 4:
    перевитрату понад hold НЕ намагаємось стягнути — за нашим власним
    моком банк finalize понад утримання й так відхиляє, підтвердити живим
    смоуком це поки не вдалось, беклог docs/SESSION_STATE.md). ОДНА
    реалізація, спільна для on_stop_transaction-шляху (kwh відомий одразу
    після StopTransaction) і кроку 3 звірки (kwh читається з уже
    'completed' сесії) — не дві копії тієї самої формули.

    kwh=None (абсурдна дельта лічильника, 3b) -> вартість 0: як kWh-модель
    звільняє ПОВНИЙ резерв при kwh=None (не вигадуємо, скільки спожито),
    так і тут — 0 означає, що подальший виклик піде через cancel_invoice()
    (повне повернення), а не через finalize() на вигадану суму.
    """
    if kwh is None:
        return Decimal("0.00")

    tariff_start = Decimal(str(tariff_uah_start)) if tariff_uah_start is not None else Decimal("0")
    tariff_kwh = Decimal(str(tariff_uah_kwh))
    cost = (tariff_start + tariff_kwh * Decimal(str(kwh))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    reserved = Decimal(str(reserved_uah))
    if cost > reserved:
        logger.error(
            "Модель B: факт %s грн перевищує утримане %s грн — перевитрата НЕ "
            "стягується автоматично, капуємо на утримане",
            cost, reserved,
        )
        return reserved
    return cost


async def complete_ocpp_transaction_and_release_uah(
    operator_id: int, transaction_id: int, kwh, meter_stop_wh, ended_at,
    reservation_id: int, reserved_uah, invoice_id: str,
    tariff_uah_start, tariff_uah_kwh, operator_token: str,
) -> bool:
    """
    UAH-аналог repo.complete_ocpp_transaction_and_release() (модель A) —
    викликається з on_stop_transaction (app/api/ocpp_ws.py) для резервацій
    з payment_method='uah'.

    НА ВІДМІНУ від kWh-моделі тут НЕМАЄ єдиної атомарної Postgres-
    транзакції на ввесь ланцюг: виклик банку (HTTP) не можна безпечно
    покласти в ту саму транзакцію, що локальний запис (docs/plan-3c-ii.md,
    розділ 2, «джерело істини — банк, не наша БД»). Порядок:

      1. repo.complete_ocpp_transaction() — той самий мʼютекс на рівні
         сесії, що й для kWh; ретрай (сесія вже 'completed') зупиняє
         функцію одразу.
      2. repo.claim_reservation_for_settlement() — атомарний ЛОКАЛЬНИЙ
         claim 'active' -> 'settling' (головний захист від подвійного
         дзвінка в банк, розділ 2 плану, п.1).
      3. compute_uah_settlement_amount() — чиста функція, капована сума.
      4. Банк: finalize_invoice() (сума > 0) або cancel_invoice() (сума
         == 0 — kwh=None або факт квантується в 0).
      5. За успіху дзвінка в банк — record_uah_settlement('finalized').
         Якщо банк недоступний (MonobankError) — рядок лишається
         'settling'; це НЕ перевикидається назовні й НЕ ламає відповідь
         OCPP станції (settle — не частина протоколу StopTransaction) —
         крок 3 звірки (reconcile_charging_reservations.py) довершить
         пізніше, перепитавши банк.

    Повертає True, якщо ЦЕЙ виклик довів розрахунок до 'finalized'; False —
    ретрай (сесія вже 'completed') або гонка (claim не дістався цьому
    виклику) або банк недоступний зараз.
    """
    became_completed = await repo.complete_ocpp_transaction(
        operator_id, transaction_id, kwh, meter_stop_wh, ended_at,
    )
    if not became_completed:
        return False

    claim = await repo.claim_reservation_for_settlement(operator_id, reservation_id)
    if claim is None:
        logger.warning(
            "Модель B: резервація #%s не в статусі 'active' (гонка зі звіркою?) — "
            "claim не дістався цьому виклику", reservation_id,
        )
        return False

    amount_uah = compute_uah_settlement_amount(tariff_uah_start, tariff_uah_kwh, kwh, reserved_uah)

    try:
        if amount_uah > 0:
            await finalize_invoice(operator_token, invoice_id, amount_uah)
        else:
            await cancel_invoice(operator_token, invoice_id)
    except MonobankError as e:
        logger.error(
            "Модель B: резервація #%s — банк недоступний під час розрахунку (%s) — "
            "рядок лишається 'settling', довершить reconcile_charging_reservations.py",
            reservation_id, e,
        )
        return False

    await repo.record_uah_settlement(operator_id, reservation_id, "finalized", amount_uah)
    logger.info("💳 Модель B: резервація #%s розрахована — %s грн (утримано було %s)",
                reservation_id, amount_uah, reserved_uah)
    return True
