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
"""
import logging
import secrets

from fastapi import FastAPI, Header, HTTPException, Request

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Mock Monobank Acquiring")

# invoiceId -> стан інвойсу
_invoices = {}


@app.post("/api/merchant/invoice/create")
async def create_invoice(request: Request, x_token: str = Header(None)):
    if not x_token:
        raise HTTPException(status_code=403, detail="X-Token required")

    body = await request.json()
    amount = body.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        raise HTTPException(status_code=400, detail="amount має бути цілим у копійках")

    invoice_id = secrets.token_hex(8)
    _invoices[invoice_id] = {
        "invoiceId": invoice_id,
        "status": "created",
        "amount": amount,
        "ccy": body.get("ccy"),
        "reference": (body.get("merchantPaymInfo") or {}).get("reference"),
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
