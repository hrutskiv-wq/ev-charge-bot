"""
Клієнт Monobank Acquiring API для білінгу операторів.

ВАЖЛИВО, чим це відрізняється від app/api/payments.py:
той модуль обслуговує «Банку» (jar) eVolt — водій переказує кошти НАМ, а
Telegram ID зашитий у коментарі переказу. Тут інша модель: інвойс створює
кожен ОПЕРАТОР своїм власним токеном мерчанта, гроші йдуть напряму йому, а
ми — лише софт (див. docs/evolt-white-label-bilinh-ta-p2p.md, розділ
«Ризики»: MVP свідомо не робить нас платіжним посередником). Тому:

  * токен береться з operators.monobank_token_encrypted, а не з env;
  * базовий URL виноситься в env, щоб тести й локальна розробка ходили
    в mock_monobank.py, а не в живий банк;
  * суми рахуються в Decimal і передаються в КОПІЙКАХ (int), як вимагає
    API — float для грошей тут не з'являється взагалі.

Документація: https://monobank.ua/api-docs/acquiring/
"""
import logging
import os
from decimal import Decimal, ROUND_HALF_UP

import httpx

logger = logging.getLogger(__name__)

# Дозволяє підмінити банк на mock_monobank.py локально й у тестах.
BASE_URL = os.getenv("MONOBANK_ACQUIRING_BASE_URL", "https://api.monobank.ua").rstrip("/")

CREATE_INVOICE_PATH = "/api/merchant/invoice/create"
INVOICE_STATUS_PATH = "/api/merchant/invoice/status"
MERCHANT_DETAILS_PATH = "/api/merchant/details"
FINALIZE_INVOICE_PATH = "/api/merchant/invoice/finalize"
CANCEL_INVOICE_PATH = "/api/merchant/invoice/cancel"

DEFAULT_TIMEOUT = 15.0

# Скільки живе інвойс. Довше тримати немає сенсу: водій стоїть біля
# станції, а «висячі» інвойси ускладнюють звірку.
INVOICE_TTL_SECONDS = 900  # 15 хвилин


class MonobankError(RuntimeError):
    """Банк відповів помилкою або недоступний."""


def uah_to_kopecks(amount_uah) -> int:
    """
    Гривні -> копійки (int), як вимагає API.

    Через Decimal з ROUND_HALF_UP, а не int(amount * 100): float-множення
    дає 19.99 * 100 == 1998.9999999999998, тобто int() зрізав би до 1998 —
    водій платив би на копійку менше, ніж показала сторінка, і звірка
    сесій з інвойсами розходилась би на рівному місці.
    """
    return int(
        (Decimal(str(amount_uah)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def kopecks_to_uah(amount_kopecks: int) -> Decimal:
    """Копійки -> гривні як Decimal (для запису в NUMERIC(12,2))."""
    return (Decimal(int(amount_kopecks)) / 100).quantize(Decimal("0.01"))


async def create_invoice(operator_token: str, amount_uah, reference: str,
                         redirect_url: str, webhook_url: str,
                         destination: str = None, payment_type: str = "debit") -> dict:
    """
    Створює інвойс у мерчанта ОПЕРАТОРА.

    reference — наш ідентифікатор платежу (id рядка operator_payments), банк
    повертає його назад незмінним. Використовується для звірки, але НЕ як
    підстава довіряти webhook: статус ми в будь-якому разі перепитуємо в
    банку (див. app/api/operator_webhook.py).

    payment_type: "debit" (звичайна оплата, замовчування) або "hold"
    (Модель B, Промпт 3c-ii — гроші БЛОКУЮТЬСЯ на картці водія, а не
    списуються одразу; подальше списання/повернення — finalize_invoice()/
    cancel_invoice() нижче, звірено живим смоуком 28-29.07.2026, див.
    docs/SESSION_STATE.md). Поле `paymentType` додається в тіло запиту
    ЛИШЕ коли воно НЕ "debit" — щоб наявні debit-виклики (app/api/
    driver_qr.py) лишались байт-в-байт тим самим payload, що й до цієї
    зміни.

    Повертає dict банку: {'invoiceId': ..., 'pageUrl': ...}.
    """
    payload = {
        "amount": uah_to_kopecks(amount_uah),
        "ccy": 980,  # ISO 4217, гривня
        "merchantPaymInfo": {
            "reference": str(reference),
            "destination": destination or "Оплата зарядної сесії",
        },
        "redirectUrl": redirect_url,
        "webHookUrl": webhook_url,
        "validity": INVOICE_TTL_SECONDS,
    }
    if payment_type != "debit":
        payload["paymentType"] = payment_type

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BASE_URL}{CREATE_INVOICE_PATH}",
                json=payload,
                headers={"X-Token": operator_token},
                timeout=DEFAULT_TIMEOUT,
            )
    except httpx.HTTPError as e:
        raise MonobankError(f"Monobank недоступний при створенні інвойсу: {e}") from e

    if resp.status_code != 200:
        # Тіло відповіді банку логуємо, токен — НІКОЛИ.
        raise MonobankError(
            f"Monobank відхилив створення інвойсу (HTTP {resp.status_code}): {resp.text}"
        )

    data = resp.json()
    if not data.get("invoiceId"):
        raise MonobankError(f"Monobank повернув відповідь без invoiceId: {data}")
    return data


async def get_invoice_status(operator_token: str, invoice_id: str) -> dict:
    """
    Питає банк про фактичний статус інвойсу.

    Це ЄДИНЕ джерело правди про оплату. Тіло webhook ми не використовуємо
    взагалі — воно лише сигнал «піди перевір». Так підробити оплату
    неможливо в принципі: навіть маючи URL webhook і знаючи invoiceId,
    зловмисник не може змусити банк сказати 'success'.

    Статуси Monobank: created / processing / hold / success / failure /
    reversed / expired.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}{INVOICE_STATUS_PATH}",
                params={"invoiceId": invoice_id},
                headers={"X-Token": operator_token},
                timeout=DEFAULT_TIMEOUT,
            )
    except httpx.HTTPError as e:
        raise MonobankError(f"Monobank недоступний при перевірці інвойсу: {e}") from e

    if resp.status_code != 200:
        raise MonobankError(
            f"Monobank не віддав статус інвойсу {invoice_id} (HTTP {resp.status_code}): {resp.text}"
        )
    return resp.json()


async def finalize_invoice(operator_token: str, invoice_id: str, amount_uah) -> dict:
    """
    Модель B (Промпт 3c-ii): часткове чи повне списання нефіналізованого
    hold. amount_uah — ФАКТИЧНО спожита вартість (finalAmount), НЕ
    утримана сума (amount) — і має бути не більшою за неї.

    ПІДТВЕРДЖЕНО ЖИВИМ СМОУКОМ 30.07.2026: банк дійсно відхиляє finalize на
    суму БІЛЬШУ за утримання (HTTP 400, errCode "1001", errText
    "finalization amount exceeds hold amount") — раніше це було
    консервативне припущення (28-29.07.2026 фіналізували лише МЕНШУ за
    hold суму). Повторний finalize того самого інвойсу банк теж
    відхиляє — той самий errCode "1001", errText "order on hold not
    found".

    Відповідь банку на сам виклик (`{"status": "success"}`) — ПІДТВЕРДЖЕНО
    ЖИВИМ СМОУКОМ 30.07.2026 (раніше взято з документації банку без живого
    підтвердження). Форма результуючого invoice/status (amount/
    finalAmount/fee/rrn/tranId) — ЖИВА, звірена смоуком. Одразу після
    finalize invoice/status короткочасно повертає ТРАНЗИТНИЙ
    `status: "processing"` з `finalAmount`, що дорівнює ПОВНІЙ утриманій
    сумі (не фактично списаній) — коректний `finalAmount` з'являється
    лише разом зі `status: "success"`.
    """
    payload = {"invoiceId": invoice_id, "amount": uah_to_kopecks(amount_uah)}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BASE_URL}{FINALIZE_INVOICE_PATH}",
                json=payload,
                headers={"X-Token": operator_token},
                timeout=DEFAULT_TIMEOUT,
            )
    except httpx.HTTPError as e:
        raise MonobankError(f"Monobank недоступний при фіналізації інвойсу {invoice_id}: {e}") from e

    if resp.status_code != 200:
        raise MonobankError(
            f"Monobank відхилив фіналізацію інвойсу {invoice_id} (HTTP {resp.status_code}): {resp.text}"
        )
    return resp.json()


async def cancel_invoice(operator_token: str, invoice_id: str) -> dict:
    """
    Модель B (Промпт 3c-ii): скасування — працює і на `success` (повернення
    вже списаного), і на НЕфіналізованому `hold` (звільнення утриманого без
    жодного списання). Друге і було головним невідомим гейта Моделі B,
    підтверджене живим смоуком 28-29.07.2026 (docs/SESSION_STATE.md):
    cancel на нефіналізованому hold СПРАЦЮВАВ, підтверджено з обох боків
    (картка «Скасування +20 ₴» + виписка мерчанта).
    """
    payload = {"invoiceId": invoice_id}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BASE_URL}{CANCEL_INVOICE_PATH}",
                json=payload,
                headers={"X-Token": operator_token},
                timeout=DEFAULT_TIMEOUT,
            )
    except httpx.HTTPError as e:
        raise MonobankError(f"Monobank недоступний при скасуванні інвойсу {invoice_id}: {e}") from e

    if resp.status_code != 200:
        raise MonobankError(
            f"Monobank відхилив скасування інвойсу {invoice_id} (HTTP {resp.status_code}): {resp.text}"
        )
    return resp.json()


async def verify_merchant_token(operator_token: str) -> bool:
    """
    Підтверджує токен реальним зверненням до банку (GET
    /api/merchant/details) — найсильніший автоматичний сигнал довіри для
    самообслуговуваного онбордингу оператора: пройти цю перевірку може лише
    той, у кого справді є живий мерчант-акаунт Monobank Acquiring.

    Повертає True/False лише коли банк дав ОДНОЗНАЧНУ відповідь:
      * True — HTTP 200, токен валідний;
      * False — HTTP 401/403, банк явно відхилив токен (це і є "недійсний
        токен", а не помилка виклику).
    Будь-що інше (мережевий збій, таймаут, 5xx, неочікуваний код) —
    MonobankError: тут НЕВІДОМО, токен поганий чи банк тимчасово
    недоступний, тож викликач має лишити оператора в 'pending' із
    нейтральним "спробуйте пізніше", а не позначати токен невалідним.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}{MERCHANT_DETAILS_PATH}",
                headers={"X-Token": operator_token},
                timeout=DEFAULT_TIMEOUT,
            )
    except httpx.HTTPError as e:
        raise MonobankError(f"Monobank недоступний при перевірці токена: {e}") from e

    if resp.status_code in (401, 403):
        return False
    if resp.status_code != 200:
        raise MonobankError(
            f"Monobank повернув неочікуваний статус при перевірці токена (HTTP {resp.status_code}): {resp.text}"
        )
    return True
