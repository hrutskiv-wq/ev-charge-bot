"""
Локальний мок Monobank Acquiring API — за зразком mock_cpo.py.

Навіщо: створення інвойсів і перевірка статусу відбуваються токеном
МЕРЧАНТА-ОПЕРАТОРА, тобто протестувати їх проти живого банку без реального
оператора з реальним рахунком неможливо. Мок дає повний прохід флоу
локально: створити інвойс -> «оплатити» -> отримати success.

Запуск:
    uvicorn mock_monobank:app --port 8081

І в .env застосунку:
    MONOBANK_ACQUIRING_BASE_URL=http://127.0.0.1:8081

Ручна «оплата» інвойсу (те, що в житті робить водій карткою):
    curl -X POST http://127.0.0.1:8081/mock/pay/<invoiceId>
Провал оплати:
    curl -X POST "http://127.0.0.1:8081/mock/pay/<invoiceId>?result=failure"

Це НЕ продакшн-код: жодної перевірки токена по суті, стан у пам'яті
процесу. Мета — відтворити контракт API, а не банк.

ЗВІРЕНО З РЕАЛЬНОЮ ВІДПОВІДДЮ БАНКУ (перший живий платіж, 2026-07-22,
сесія #1, 20 грн, status=success) — форма `invoice/status` доповнена
полями, яких раніше не було: `payMethod`, `createdDate`, `destination`,
`finalAmount`, `paymentInfo` (вкладений об'єкт), `modifiedDate`. Наш код
(app/api/operator_webhook.py) читає лише `status`/`amount` — обидва вже
збігались і до цієї звірки, нових розбіжностей рівня бага не знайдено
(див. review_prompt-fix-mock-vs-reality.md за деталями). `destination`
тепер справді береться з тіла запиту, а не губиться, як було. Значення
всередині `paymentInfo` — вигадані заглушки за формою реальних (fee,
rrn, bank, tranId, country, terminal, maskedPan, approvalCode,
paymentMethod, paymentSystem), а не справжні банківські дані з проду.

НЕ ПЕРЕВІРЕНО: форма відповіді для статусів failure/expired/reversed —
живих зразків цих статусів поки немає, тому `paymentInfo`/`finalAmount`/
`payMethod` мок для них НЕ додає (консервативно, щоб не видавати
непідтверджене здогадування за факт). Звірити, коли трапиться перший
живий провал оплати.
"""
import logging
import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Mock Monobank Acquiring")

# invoiceId -> стан інвойсу
_invoices = {}


def _now_iso() -> str:
    """Формат банку: '2026-07-22T11:25:16Z' — без мікросекунд, з Z замість +00:00."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Заглушкові дані картки/транзакції для paymentInfo — форма як у реального
# банку, значення вигадані (не справжні реквізити з жодного платежу).
_FAKE_PAYMENT_INFO = {
    "maskedPan": "444111******6969",
    "approvalCode": "000000",
    "rrn": "000000000000",
    "tranId": "0000000000",
    "terminal": "MI000000",
    "bank": "Mock Bank",
    "paymentSystem": "visa",
    "paymentMethod": "monobank",
    "fee": 0,
    "country": "804",
}


@app.post("/api/merchant/invoice/create")
async def create_invoice(request: Request, x_token: str = Header(None)):
    if not x_token:
        raise HTTPException(status_code=403, detail="X-Token required")

    body = await request.json()
    amount = body.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        raise HTTPException(status_code=400, detail="amount має бути цілим у копійках")

    merchant_info = body.get("merchantPaymInfo") or {}
    invoice_id = secrets.token_hex(8)
    now = _now_iso()
    _invoices[invoice_id] = {
        "invoiceId": invoice_id,
        "status": "created",
        "amount": amount,
        "ccy": body.get("ccy"),
        "reference": merchant_info.get("reference"),
        "destination": merchant_info.get("destination"),
        "createdDate": now,
        "modifiedDate": now,
        # токен зберігаємо лише щоб перевірити, що статус питають тим самим
        # мерчантом, який створив інвойс — як у справжньому банку
        "_token": x_token,
    }
    logging.info("🧾 Створено інвойс %s на %s коп.", invoice_id, amount)
    return {
        "invoiceId": invoice_id,
        "pageUrl": f"http://127.0.0.1:8081/mock/page/{invoice_id}",
    }


@app.get("/api/merchant/invoice/status")
async def invoice_status(invoiceId: str, x_token: str = Header(None)):
    invoice = _invoices.get(invoiceId)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    if invoice["_token"] != x_token:
        # Саме так справжній банк не дасть одному мерчанту читати інвойси
        # іншого — мок відтворює цю межу навмисно.
        raise HTTPException(status_code=403, detail="foreign invoice")
    return {k: v for k, v in invoice.items() if not k.startswith("_")}


@app.post("/mock/pay/{invoice_id}")
async def mock_pay(invoice_id: str, result: str = "success"):
    """Імітує дію водія: оплату (або провал) інвойсу."""
    invoice = _invoices.get(invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    invoice["status"] = result
    invoice["modifiedDate"] = _now_iso()
    if result == "success":
        # Поля нижче підтверджені живим платежем лише для success — див.
        # докстрінг модуля. Для інших статусів навмисно не додаємо.
        invoice["finalAmount"] = invoice["amount"]
        invoice["payMethod"] = "monobank"
        invoice["paymentInfo"] = dict(_FAKE_PAYMENT_INFO)
    logging.info("💳 Інвойс %s переведено в статус %s", invoice_id, result)
    return {"invoiceId": invoice_id, "status": result}


@app.get("/mock/page/{invoice_id}")
async def mock_page(invoice_id: str):
    """Заглушка сторінки оплати Monobank."""
    return {
        "info": "Сторінка оплати Monobank (мок)",
        "invoiceId": invoice_id,
        "pay": f"POST /mock/pay/{invoice_id}",
    }
