"""
Webhook про оплату інвойсу, створеного еквайрингом ОПЕРАТОРА.

Модель довіри — «не віримо webhook, перепитуємо банк»:

    POST /webhook/operator/{operator_id}
      -> дістаємо invoiceId з тіла
      -> перевіряємо, що такий інвойс справді існує в operator_payments
         САМЕ ЦЬОГО operator_id (з URL)
      -> питаємо банк GET /api/merchant/invoice/status токеном оператора
      -> віримо ЛИШЕ відповіді банку

Тіло webhook не використовується ні для статусу, ні для суми. Причина: ці
інвойси створені токенами різних мерчантів, тож x-sign кожного підписаний
ключем відповідного оператора, і глобальна перевірка з app/api/payments.py
(наш власний MONOBANK_API_TOKEN) для них не працює в принципі. Замість
того щоб вести кеш публічних ключів на кожного оператора, ми просто
робимо тіло webhook нерелевантним: навіть маючи URL і знаючи invoiceId,
підробити оплату неможливо — банк не скаже 'success', поки грошей немає.

Невідомий invoiceId -> тихий 200 без подробиць: відповідь не має
відрізнятись для «інвойсу не існує», «інвойс чужого оператора» і «все
гаразд», інакше ендпоінт перетворюється на оракул для зондування.
"""
import json
import logging

from fastapi import APIRouter, Request, Response, status

from app.core.crypto import EncryptionKeyMissing, decrypt_secret
from app.database import operators_repo as repo
from app.services.monobank_acquiring import (
    MonobankError,
    get_invoice_status,
    uah_to_kopecks,
)
from app.services.operator_notify import notify_operator_paid

logger = logging.getLogger(__name__)

operator_webhook_router = APIRouter()

# Статус банку -> наш статус у operator_payments.
# created/processing/hold — проміжні, нічого не робимо й чекаємо
# наступного webhook.
_FINAL_STATUS_MAP = {
    "success": "success",
    "failure": "failed",
    "expired": "expired",
    "reversed": "reversed",
}

# Тихе підтвердження: Monobank припиняє ретраї, а зловмисник не дізнається
# нічого про те, чи існує інвойс.
_QUIET_OK = Response(status_code=status.HTTP_200_OK)


async def complete_paid_session(operator_id: int, payment: dict) -> str:
    """
    "Хвіст" платіжного ланцюга, коли payment['status'] уже 'success' у нашій
    БД (байдуже, підтвердив webhook щойно чи звірка reconcile_operators.py
    — Промпт 5): сесія -> paid, дохід через record_session_income (сам
    ідемпотентний), пуш оператору. Винесено окремою функцією, щоб один і той
    самий код відпрацьовував в обох місцях, а не дублювався.

    Повертає 'credited' або 'no_session' (сесія до платежу не привʼязана —
    нараховувати дохід нікуди, потрібен ручний розбір).
    """
    session = await repo.get_session_by_payment(operator_id, payment["id"])
    if session is None:
        logger.error(
            "Оператор %s: платіж %s успішний, але до нього не привʼязана "
            "сесія — дохід не проведено, потрібен ручний розбір",
            operator_id, payment["id"],
        )
        return "no_session"

    operator = await repo.get_operator(operator_id)
    commission_pct = operator["commission_pct"] if operator else 0

    await repo.set_session_status(operator_id, session["id"], "paid")
    # record_session_income сам ідемпотентний (uq_ledger_session_income),
    # тож навіть за гонки нарахування не задвоїться.
    await repo.record_session_income(
        operator_id, session["id"], payment["amount_uah"], commission_pct,
    )

    logger.info(
        "💳 Оператор %s: платіж %s, сесія #%s -> paid, дохід проведено",
        operator_id, payment["id"], session["id"],
    )

    # Пуш оператору «Оплачено, увімкніть станцію». Свідомо ОСТАННІМ кроком і
    # без права зламати відповідь: гроші вже прийшли, сесія позначена
    # оплаченою, дохід проведено. Недоступний Telegram не привід повертати
    # помилку викликачу (webhook чи звірці) і провокувати повторну обробку
    # вже проведеного платежу.
    if operator and operator["telegram_id"]:
        try:
            station = await repo.get_station(operator_id, session["station_id"])
            await notify_operator_paid(
                telegram_id=operator["telegram_id"],
                operator_id=operator_id,
                session_id=session["id"],
                station_name=station["name"] if station else "станція",
                amount_uah=payment["amount_uah"],
                driver_contact=session["driver_contact"],
            )
        except Exception as e:
            logger.error("Оператор %s: збій сповіщення про сесію #%s: %s",
                         operator_id, session["id"], e)

    return "credited"


async def apply_bank_status(operator_id: int, payment: dict, invoice: dict) -> str:
    """
    Єдина точка інтерпретації відповіді банку на конкретний платіж —
    використовується і webhook'ом (нижче), і звіркою
    (reconcile_operators.py, Промпт 5), щоб тіло банку трактувалось
    ОДНАКОВО в обох місцях, а не двома трохи різними копіпастами.

    Повертає один з: 'unknown_status' (проміжний стан банку — created/
    processing/hold, нічого не робимо), 'status_updated' (фінальний
    негативний статус записано), 'amount_mismatch' (банк каже "оплачено",
    але не на ту суму — нарахування НЕ проведено), 'already_processed'
    (платіж уже 'success' — гонка з іншим викликом), 'credited' /
    'no_session' (див. complete_paid_session).
    """
    bank_status = (invoice.get("status") or "").strip()
    mapped = _FINAL_STATUS_MAP.get(bank_status)
    if mapped is None:
        logger.info("Оператор %s: платіж %s ще в проміжному стані банку '%s'",
                    operator_id, payment["id"], bank_status)
        return "unknown_status"

    payload_json = json.dumps(invoice, ensure_ascii=False)

    if mapped != "success":
        await repo.set_operator_payment_status(operator_id, payment["id"], mapped,
                                               payload=payload_json)
        logger.info("Оператор %s: платіж %s -> %s", operator_id, payment["id"], mapped)
        return "status_updated"

    # Сума з банку має збігатися з тією, на яку ми виставляли інвойс.
    # Розбіжність означає, що щось пішло не так на боці банку або в наших
    # даних — краще не нарахувати, ніж нарахувати не те.
    expected_kopecks = uah_to_kopecks(payment["amount_uah"])
    actual_kopecks = invoice.get("amount")
    if actual_kopecks is not None and int(actual_kopecks) != expected_kopecks:
        logger.error(
            "Оператор %s: платіж %s оплачений на %s коп., а очікувалось %s коп. "
            "— нарахування НЕ проведено, потрібен ручний розбір",
            operator_id, payment["id"], actual_kopecks, expected_kopecks,
        )
        return "amount_mismatch"

    # Умова `status <> 'success'` усередині UPDATE працює як мʼютекс: рівно
    # один паралельний виклик (webhook або звірка) отримає True і проведе
    # нарахування, решта побачать False і вийдуть.
    became_success = await repo.set_operator_payment_status(
        operator_id, payment["id"], "success", payload=payload_json,
    )
    if not became_success:
        logger.info("Оператор %s: платіж %s уже проведений паралельно",
                    operator_id, payment["id"])
        return "already_processed"

    return await complete_paid_session(operator_id, payment)


@operator_webhook_router.post("/webhook/operator/{operator_id}")
async def operator_invoice_webhook(operator_id: int, request: Request):
    try:
        raw_body = await request.body()
        payload = json.loads(raw_body) if raw_body else {}
    except Exception:
        logger.warning("Webhook оператора %s: тіло не є валідним JSON", operator_id)
        return _QUIET_OK

    invoice_id = (payload.get("invoiceId") or "").strip() if isinstance(payload, dict) else ""
    if not invoice_id:
        logger.warning("Webhook оператора %s: у тілі немає invoiceId", operator_id)
        return _QUIET_OK

    # 1. Інвойс має належати саме тому оператору, що в URL.
    payment = await repo.get_operator_payment_by_invoice(operator_id, invoice_id)
    if payment is None:
        logger.warning(
            "Webhook оператора %s: інвойс %s не знайдено серед платежів цього "
            "оператора — ігноруємо", operator_id, invoice_id,
        )
        return _QUIET_OK

    # 2. Уже проведений платіж повторно не чіпаємо і банк не смикаємо.
    if payment["status"] == "success":
        logger.info(
            "Webhook оператора %s: інвойс %s уже проведений — повтор проігноровано",
            operator_id, invoice_id,
        )
        return _QUIET_OK

    # 3. Токен оператора потрібен, щоб спитати банк.
    token_encrypted = await repo.get_operator_monobank_token_encrypted(operator_id)
    if not token_encrypted:
        logger.error(
            "Webhook оператора %s: інвойс %s є, але еквайринг-токен оператора не "
            "збережений — підтвердити оплату неможливо", operator_id, invoice_id,
        )
        return _QUIET_OK

    try:
        operator_token = decrypt_secret(token_encrypted)
    except (EncryptionKeyMissing, ValueError) as e:
        logger.error("Webhook оператора %s: не вдалося розшифрувати токен: %s",
                     operator_id, e)
        return _QUIET_OK

    # 4. Єдине джерело правди — відповідь банку.
    try:
        invoice = await get_invoice_status(operator_token, invoice_id)
    except MonobankError as e:
        # Банк недоступний — віддаємо 500, щоб Monobank повторив webhook
        # пізніше, а оплата не загубилась.
        logger.error("Webhook оператора %s: не вдалося перевірити інвойс %s: %s",
                     operator_id, invoice_id, e)
        return Response(status_code=status.HTTP_502_BAD_GATEWAY)

    # 5. Уся інтерпретація відповіді банку (проміжний/фінальний статус, звірка
    # суми, мʼютекс на 'success', сесія -> paid, дохід, пуш) — в одній
    # спільній функції apply_bank_status(), яку викликає і звірка
    # reconcile_operators.py (Промпт 5). Значення, яке вона повертає, тут не
    # впливає на відповідь: webhook завжди тихо підтверджує (200), щоб
    # Monobank не ретраїв уже оброблений або свідомо відкладений випадок —
    # розбіжності, які webhook не добере (банк більше не ретраїть, процес
    # впав між кроками), знайде звірка `reconcile_operators.py` (Промпт 5).
    await apply_bank_status(operator_id, payment, invoice)

    return _QUIET_OK
