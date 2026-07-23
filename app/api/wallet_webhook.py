"""
Webhook про оплату поповнення kWh-гаманця водія (buy-side, Monobank-еквайринг
оператора №0 — конфігурується env WALLET_OPERATOR_ID).

    POST /webhook/wallet/{operator_id}

Навмисно ОКРЕМИЙ роут від /webhook/operator/{operator_id}
(app/api/operator_webhook.py), а не гілка в ньому — Monobank викликає рівно
той webhook_url, який ми дали при створенні інвойсу, тож розрізнення
wallet-поповнення й станційної сесії відбувається на рівні роутингу, а не
парсингом тіла. Це лишає станційний webhook і його тести (test_operator_
payments.py) абсолютно недоторканими: жодного нового виклику в тому шляху.

Та сама модель довіри, що й у станційному webhook — тілу не віримо,
перепитуємо банк токеном оператора (див. app/api/operator_webhook.py,
докладний докстрінг там же). Невідомий invoiceId -> тихий 200.
"""
import json
import logging

from fastapi import APIRouter, Request, Response, status

from app.core.crypto import EncryptionKeyMissing, decrypt_secret
from app.database import connection as db_conn
from app.database import operators_repo as repo
from app.services.monobank_acquiring import (
    MonobankError,
    get_invoice_status,
    uah_to_kopecks,
)

logger = logging.getLogger(__name__)

wallet_webhook_router = APIRouter()

# Той самий словник фінальних статусів, що й у станційному webhook.
_FINAL_STATUS_MAP = {
    "success": "success",
    "failure": "failed",
    "expired": "expired",
    "reversed": "reversed",
}

_QUIET_OK = Response(status_code=status.HTTP_200_OK)


async def _notify_driver_credited(user_id: int, kwh, amount_uah) -> bool:
    """
    Пуш водію «Баланс поповнено». Відкладений імпорт bot — та сама причина,
    що й у app/services/operator_notify.py: платіжні тести не мають тягнути
    aiogram і живий BOT_TOKEN. Збій сповіщення не має скасовувати вже
    проведене нарахування.
    """
    try:
        from app.core.loader import bot

        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 <b>Баланс поповнено!</b>\n\n"
                f"🔋 Нараховано: <b>{kwh} кВт·год</b>\n"
                f"💳 Оплачено: <b>{amount_uah} грн</b>"
            ),
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        logger.error("Не вдалося сповістити водія %s про поповнення гаманця: %s",
                     user_id, e)
        return False


async def _credit_wallet_topup_in_conn(conn, topup: dict) -> str:
    """
    "Ядро" нарахування: рядок у payments (щоб kw_transactions.payment_id
    мав на що посилатись — та ж таблиця, що й для Telegram-інвойсу та
    Monobank-Банки, FK на wallet_topups тут не підходить, бо це різні
    таблиці), потім update_user_balance (ЄДИНА точка запису балансу).

    НАВМИСНО не відкриває власного conn/transaction і не шле пуш — це
    відповідальність викликача. Причина: викликається як частина ОДНІЄЇ
    транзакції разом із мʼютексом set_wallet_topup_status(conn=...) у
    apply_wallet_topup_status() — блокер #1 незалежного рев'ю: якщо статус
    'success' і фактичне нарахування комітяться ОКРЕМИМИ транзакціями,
    крах процесу між ними лишає поповнення 'success' без грошей, а
    повторний webhook бачить уже 'success' і тихо виходить — гроші клієнта
    губляться безслідно (для wallet_topups немає звірки, на відміну від
    operator_payments/reconcile_operators.py). В одній транзакції такий
    крах відкочує ОБИДВА кроки — повторний webhook побачить 'pending' і
    добере нарахування чисто.

    ON CONFLICT (invoice_id) DO NOTHING на payments — друга лінія захисту
    від подвійного нарахування (перша — мʼютекс set_wallet_topup_status).
    Повертає 'credited' або 'already_processed'.

    Явний контракт: conn МАЄ бути всередині активної транзакції
    (conn.transaction()) — інакше атомарність мʼютекс+нарахування (див.
    вище) ламається мовчки для будь-якого майбутнього викликача, який про
    неї не знав. is_in_transaction() (asyncpg) перевіряється на вході й
    падає голосно замість того, щоб довіряти самій лише конвенції.
    """
    if not conn.is_in_transaction():
        raise RuntimeError(
            "_credit_wallet_topup_in_conn викликано поза активною транзакцією — "
            "мʼютекс set_wallet_topup_status і нарахування мають комітитись "
            "атомарно (обгорніть виклик у conn.transaction())."
        )

    payment_id = await conn.fetchval(
        """
        INSERT INTO payments (user_id, invoice_id, amount, provider, status, payload, created_at, updated_at)
        VALUES ($1, $2, $3, 'monobank', 'success', $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (invoice_id) DO NOTHING
        RETURNING id
        """,
        topup["user_id"], topup["invoice_id"], topup["amount_uah"],
        json.dumps({"wallet_topup_id": topup["id"], "package": topup["package"]}),
    )
    if payment_id is None:
        logger.info(
            "Поповнення гаманця: інвойс %s уже записаний у payments — "
            "нарахування не повторюємо", topup["invoice_id"],
        )
        return "already_processed"

    await db_conn.update_user_balance(
        user_id=topup["user_id"],
        amount_kwh=float(topup["kwh"]),
        t_type="deposit",
        conn=conn,
        payment_id=payment_id,
        description=f"Поповнення пакета {topup['kwh']} кВт·год через Monobank",
    )

    logger.info("🔋 Поповнення гаманця: користувач %s, +%s кВт·год (інвойс %s)",
                topup["user_id"], topup["kwh"], topup["invoice_id"])

    return "credited"


async def _push_credit_notification(topup: dict, outcome: str) -> None:
    """Пуш водію ПІСЛЯ коміту нарахування — збій не має права нічого відкотити."""
    if outcome != "credited":
        return
    try:
        await _notify_driver_credited(topup["user_id"], topup["kwh"], topup["amount_uah"])
    except Exception as e:
        logger.error("Поповнення гаманця: збій сповіщення користувача %s: %s",
                     topup["user_id"], e)


async def credit_wallet_topup(topup: dict) -> str:
    """
    Тонка обгортка над _credit_wallet_topup_in_conn для прямих викликів поза
    apply_wallet_topup_status (напр. тести): відкриває власні conn і
    транзакцію, викликає ядро нарахування, потім шле пуш.
    """
    pool = await db_conn.get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            outcome = await _credit_wallet_topup_in_conn(conn, topup)

    await _push_credit_notification(topup, outcome)
    return outcome


async def apply_wallet_topup_status(operator_id: int, topup: dict, invoice: dict) -> str:
    """
    Єдина точка інтерпретації відповіді банку для wallet-поповнення —
    дзеркало apply_bank_status() з app/api/operator_webhook.py, але для
    wallet_topups замість operator_payments.

    Повертає: 'unknown_status', 'status_updated', 'amount_mismatch',
    'already_processed', 'credited'.
    """
    bank_status = (invoice.get("status") or "").strip()
    mapped = _FINAL_STATUS_MAP.get(bank_status)
    if mapped is None:
        logger.info("Поповнення гаманця: інвойс %s ще в проміжному стані банку '%s'",
                    topup["invoice_id"], bank_status)
        return "unknown_status"

    payload_json = json.dumps(invoice, ensure_ascii=False)

    if mapped != "success":
        await repo.set_wallet_topup_status(operator_id, topup["id"], mapped,
                                           payload=payload_json)
        logger.info("Поповнення гаманця: інвойс %s -> %s", topup["invoice_id"], mapped)
        return "status_updated"

    expected_kopecks = uah_to_kopecks(topup["amount_uah"])
    actual_kopecks = invoice.get("amount")
    if actual_kopecks is not None and int(actual_kopecks) != expected_kopecks:
        logger.error(
            "Поповнення гаманця: інвойс %s оплачений на %s коп., а очікувалось %s коп. "
            "— нарахування НЕ проведено, потрібен ручний розбір",
            topup["invoice_id"], actual_kopecks, expected_kopecks,
        )
        return "amount_mismatch"

    # Мʼютекс (статус -> 'success') і фактичне нарахування — В ОДНІЙ
    # транзакції (conn=conn на обох кроках). Без цього крах процесу між
    # ними лишає поповнення 'success' без грошей — блокер #1 незалежного
    # рев'ю, докладно в докстрінгу _credit_wallet_topup_in_conn.
    pool = await db_conn.get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            became_success = await repo.set_wallet_topup_status(
                operator_id, topup["id"], "success", payload=payload_json, conn=conn,
            )
            if not became_success:
                logger.info("Поповнення гаманця: інвойс %s уже проведений паралельно",
                            topup["invoice_id"])
                return "already_processed"

            outcome = await _credit_wallet_topup_in_conn(conn, topup)

    await _push_credit_notification(topup, outcome)
    return outcome


@wallet_webhook_router.post("/webhook/wallet/{operator_id}")
async def wallet_topup_webhook(operator_id: int, request: Request):
    try:
        raw_body = await request.body()
        payload = json.loads(raw_body) if raw_body else {}
    except Exception:
        logger.warning("Wallet webhook оператора %s: тіло не є валідним JSON", operator_id)
        return _QUIET_OK

    invoice_id = (payload.get("invoiceId") or "").strip() if isinstance(payload, dict) else ""
    if not invoice_id:
        logger.warning("Wallet webhook оператора %s: у тілі немає invoiceId", operator_id)
        return _QUIET_OK

    topup = await repo.get_wallet_topup_by_invoice(operator_id, invoice_id)
    if topup is None:
        logger.warning(
            "Wallet webhook оператора %s: інвойс %s не знайдено серед поповнень "
            "гаманця — ігноруємо", operator_id, invoice_id,
        )
        return _QUIET_OK

    if topup["status"] == "success":
        logger.info(
            "Wallet webhook оператора %s: інвойс %s уже проведений — повтор проігноровано",
            operator_id, invoice_id,
        )
        return _QUIET_OK

    token_encrypted = await repo.get_operator_monobank_token_encrypted(operator_id)
    if not token_encrypted:
        logger.error(
            "Wallet webhook оператора %s: інвойс %s є, але еквайринг-токен оператора не "
            "збережений — підтвердити оплату неможливо", operator_id, invoice_id,
        )
        return _QUIET_OK

    try:
        operator_token = decrypt_secret(token_encrypted)
    except (EncryptionKeyMissing, ValueError) as e:
        logger.error("Wallet webhook оператора %s: не вдалося розшифрувати токен: %s",
                     operator_id, e)
        return _QUIET_OK

    try:
        invoice = await get_invoice_status(operator_token, invoice_id)
    except MonobankError as e:
        logger.error("Wallet webhook оператора %s: не вдалося перевірити інвойс %s: %s",
                     operator_id, invoice_id, e)
        return Response(status_code=status.HTTP_502_BAD_GATEWAY)

    await apply_wallet_topup_status(operator_id, topup, invoice)

    return _QUIET_OK
