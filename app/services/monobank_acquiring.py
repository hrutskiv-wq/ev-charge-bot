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
                         destination: str = None) -> dict:
    """
    Створює інвойс у мерчанта ОПЕРАТОРА.

    reference — наш ідентифікатор платежу (id рядка operator_payments), банк
    повертає його назад незмінним. Використовується для звірки, але НЕ як
    підстава довіряти webhook: статус ми в будь-якому разі перепитуємо в
    банку (див. app/api/operator_webhook.py).

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
