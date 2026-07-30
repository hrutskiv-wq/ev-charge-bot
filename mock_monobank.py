"""
Локальний мок Monobank Acquiring API — за зразком mock_cpo.py.

Навіщо: створення інвойсів і перевірка статусу відбуваються токеном
МЕРЧАНТА-ОПЕРАТОРА, тобто протестувати їх проти живого банку без реального
оператора з реальним рахунком неможливо. Мок дає повний прохід флоу
локально: створити інвойс -> «оплатити» -> отримати success. Тепер також —
hold (утримання) -> finalize (часткове списання) або cancel (звільнення).

Запуск:
    uvicorn mock_monobank:app --port 8081

І в .env застосунку:
    MONOBANK_ACQUIRING_BASE_URL=http://127.0.0.1:8081

Ручна «оплата» інвойсу (те, що в житті робить водій карткою):
    curl -X POST http://127.0.0.1:8081/mock/pay/<invoiceId>
Провал оплати:
    curl -X POST "http://127.0.0.1:8081/mock/pay/<invoiceId>?result=failure"
Hold (створити інвойс із paymentType: "hold", потім оплатити тим самим
/mock/pay — інвойс перейде в status=hold, а не success):
    curl -X POST http://127.0.0.1:8081/mock/pay/<invoiceId>
Фіналізація (часткове чи повне списання утриманого):
    curl -X POST http://127.0.0.1:8081/api/merchant/invoice/finalize \\
      -H "X-Token: ..." -d '{"invoiceId": "...", "amount": 500}'
Скасування (звільнення hold АБО повернення вже списаного success):
    curl -X POST http://127.0.0.1:8081/api/merchant/invoice/cancel \\
      -H "X-Token: ..." -d '{"invoiceId": "..."}'

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

ЗВІРЕНО З РЕАЛЬНИМ ЖИВИМ HOLD-СМОУКОМ (28–29.07.2026, власна картка → власний
мерчант, дві оплати по 20 грн) — це був гейт Моделі B (3c-ii): «cancel на
нефіналізованому hold не підтверджений документацією». Гейт ПРОЙДЕНО,
підтверджено з обох боків (картка + виписка мерчанта), деталі → SESSION_STATE.
Форми нижче — ЖИВІ, окрім прямо позначеного винятку:
  * статус `hold` — поля `finalAmount` НЕМАЄ ВЗАГАЛІ (не 0, не null — ключа
    немає в тілі відповіді);
  * статус `success` після ЧАСТКОВОЇ фіналізації — `amount` лишається
    початковим УТРИМАННЯМ, `finalAmount` = фактично СПИСАНЕ; `fee`
    перерахована з фактично списаного (у смоуку: 26 коп. на утриманих 2000
    -> 7 коп. на списаних 500); `rrn`/`tranId` ІНШІ, ніж на етапі hold;
    `approvalCode` той самий;
  * статус `reversed` після cancel нефіналізованого holdу — з'являється
    `cancelList` (масив), `finalAmount: 0`;
  * відповідь на сам `invoice/cancel` — `{"status": "success",
    "createdDate": ..., "modifiedDate": ...}` — ЖИВА.
  * відповідь на `invoice/finalize` — `{"status": "success"}` — ПІДТВЕРДЖЕНО
    ЖИВИМ СМОУКОМ 30.07.2026 (раніше було взято з документації банку без
    живого підтвердження).
Значення (rrn/approvalCode/tranId/terminal/maskedPan) — і тут вигадані
заглушки, НЕ справжні реквізити з проду (та сама конвенція, що вище).

ЖИВИЙ СМОУК 30.07.2026 (наскрізний — через реальний вебхук і production-код,
не лише цей мок) додав нові підтверджені факти:
  * банк шле вебхуки і на проміжних станах `created`/`processing`
    hold-інвойсу, не лише на фінальний `hold` — і навіть повторно, шторм
    ≥3 дублікатів `hold`-вебхука за ~250 мс на одну оплату (захист — наш
    власний мʼютекс `mark_reservation_hold_confirmed`, не покладання на
    банк);
  * одразу після `finalize` `invoice/status` короткочасно повертає
    ТРАНЗИТНИЙ `status: "processing"` з `finalAmount`, що дорівнює ПОВНІЙ
    утриманій сумі (не фактично списаній) — коректний `finalAmount`
    з'являється лише разом зі `status: "success"`. Полю `finalAmount` не
    можна вірити, поки статус не `success`;
  * over-capture (finalize понад hold) банк ВІДХИЛЯЄ — HTTP 400,
    `{"errCode": "1001", "errText": "finalization amount exceeds hold
    amount"}`, hold лишається цілим — тепер ФАКТ, не консервативний здогад
    (див. `finalize_invoice()` нижче);
  * повторний `finalize` того самого інвойсу банк теж ВІДХИЛЯЄ — HTTP 400,
    `{"errCode": "1001", "errText": "order on hold not found"}` (той самий
    `errCode`, що й over-capture — розрізняти можна лише за `errText`).
    Подвійного списання немає, але це аргумент на користь власного
    `'settling'`-мʼютекса (`claim_reservation_for_settlement()`), а не
    покладання на цю поведінку банку.

НЕ ПЕРЕВІРЕНО: форма відповіді для статусів failure/expired — живих
зразків цих статусів поки немає, тому `paymentInfo`/`finalAmount`/
`payMethod` мок для них НЕ додає (консервативно, щоб не видавати
непідтверджене здогадування за факт). Звірити, коли трапиться перший
живий провал оплати.
"""
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

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

# Ілюстративна ставка комісії для hold/finalize — НЕ офіційна тарифна сітка
# банку (та публічно не документована як формула, залежить від картки й
# методу оплати). Збігається з двома живими точками смоуку 28-29.07.2026
# (26 коп. на утриманих 2000, 7 коп. на списаних 500 -> 1.3% в обох), але
# мета тут лише показати НАПРЯМОК: комісія рахується від фактично
# списаного (finalAmount), а не від утриманого (amount).
_FAKE_FEE_RATE = Decimal("0.013")


def _fake_fee_kopecks(amount_kopecks: int) -> int:
    return int(
        (Decimal(amount_kopecks) * _FAKE_FEE_RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def _fake_payment_info(rrn_suffix: str, fee_kopecks: int) -> dict:
    """
    Форма як у банку, значення — вигадані заглушки (не реальні реквізити).
    `rrn`/`tranId` навмисно РІЗНІ між hold і фіналізацією (як і в живому
    смоуку 28-29.07.2026), `approvalCode` лишається тим самим — так само,
    як спостерігалось наживо.
    """
    return {
        "maskedPan": "444111******6969",
        "approvalCode": "000000",
        "rrn": f"00000000000{rrn_suffix}",
        "tranId": f"000000000{rrn_suffix}",
        "terminal": "MI000000",
        "bank": "Mock Bank",
        "paymentSystem": "visa",
        "paymentMethod": "monobank",
        "fee": fee_kopecks,
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
        # paymentType визначає, чи «оплата» веде в success (звичайний
        # дебет), чи в hold (утримання, потребує finalize/cancel) — не
        # повертається у invoice/status, тому підкреслення в імені, як і
        # _token вище (той самий фільтр приховує обидва).
        "_payment_type": body.get("paymentType", "debit"),
    }
    logging.info("🧾 Створено інвойс %s на %s коп. (paymentType=%s)",
                 invoice_id, amount, _invoices[invoice_id]["_payment_type"])
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


@app.get("/api/merchant/details")
async def merchant_details(x_token: str = Header(None)):
    """
    Мок для верифікації токена самообслуговуваного онбордингу
    (app/services/monobank_acquiring.py::verify_merchant_token()). Будь-який
    непорожній токен, окрім спеціального маркера "invalid-token" (зручно для
    ручної перевірки гілки відмови), вважається валідним мерчантом.
    """
    if not x_token:
        raise HTTPException(status_code=403, detail="X-Token required")
    if x_token == "invalid-token":
        raise HTTPException(status_code=403, detail="invalid merchant token")
    return {
        "merchantId": f"mock-{x_token[-6:]}",
        "merchantName": "Mock Merchant",
        "edrpou": "00000000",
        "workSchedule": [],
    }


@app.post("/mock/pay/{invoice_id}")
async def mock_pay(invoice_id: str, result: str = "success"):
    """Імітує дію водія: оплату (або провал) інвойсу."""
    invoice = _invoices.get(invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    invoice["modifiedDate"] = _now_iso()

    if result == "success" and invoice.get("_payment_type") == "hold":
        # ЖИВИЙ СМОУК 28-29.07.2026: на етапі hold поля finalAmount у
        # відповіді банку НЕМАЄ ВЗАГАЛІ — не додаємо його навіть як 0/null.
        invoice["status"] = "hold"
        invoice["payMethod"] = "monobank"
        invoice["paymentInfo"] = _fake_payment_info(
            rrn_suffix="1", fee_kopecks=_fake_fee_kopecks(invoice["amount"])
        )
        logging.info("🔒 Інвойс %s переведено в hold (утримано %s коп.)", invoice_id, invoice["amount"])
        return {"invoiceId": invoice_id, "status": "hold"}

    invoice["status"] = result
    if result == "success":
        # Поля нижче підтверджені живим платежем лише для success — див.
        # докстрінг модуля. Для інших статусів навмисно не додаємо.
        invoice["finalAmount"] = invoice["amount"]
        invoice["payMethod"] = "monobank"
        invoice["paymentInfo"] = dict(_FAKE_PAYMENT_INFO)
    logging.info("💳 Інвойс %s переведено в статус %s", invoice_id, result)
    return {"invoiceId": invoice_id, "status": result}


@app.post("/api/merchant/invoice/finalize")
async def finalize_invoice(request: Request, x_token: str = Header(None)):
    """
    Часткове чи повне списання нефіналізованого hold.

    Відповідь `{"status": "success"}` — ПІДТВЕРДЖЕНО ЖИВИМ СМОУКОМ
    30.07.2026 (раніше було взято з документації банку без живого
    підтвердження). Форма результуючого invoice/status
    (amount/finalAmount/fee/rrn/tranId) — теж ЖИВА (див. докстрінг модуля).
    """
    body = await request.json()
    invoice_id = body.get("invoiceId")
    amount = body.get("amount")

    invoice = _invoices.get(invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    if invoice["_token"] != x_token:
        raise HTTPException(status_code=403, detail="foreign invoice")
    # ПІДТВЕРДЖЕНО ЖИВИМ СМОУКОМ 30.07.2026: finalize не на статусі hold
    # (у т.ч. повторний finalize того самого інвойсу) банк відхиляє тим
    # самим errCode, що й over-capture нижче — розрізнити можна лише за
    # errText.
    if invoice["status"] != "hold":
        raise HTTPException(status_code=400, detail={
            "errCode": "1001",
            "errText": "order on hold not found",
        })
    # ПІДТВЕРДЖЕНО ЖИВИМ СМОУКОМ 30.07.2026: over-capture (finalize на суму
    # БІЛЬШУ за hold) банк дійсно відхиляє — HTTP 400, errCode "1001".
    if not isinstance(amount, int) or amount <= 0:
        raise HTTPException(status_code=400, detail={
            "errCode": "1001",
            "errText": "amount має бути цілим додатним числом копійок",
        })
    if amount > invoice["amount"]:
        raise HTTPException(status_code=400, detail={
            "errCode": "1001",
            "errText": "finalization amount exceeds hold amount",
        })

    invoice["status"] = "success"
    invoice["finalAmount"] = amount
    invoice["modifiedDate"] = _now_iso()
    # amount лишається початковим УТРИМАННЯМ (не перезаписуємо) — так само,
    # як спостерігалось наживо: hold=2000 -> finalize(500) -> amount
    # лишився 2000, finalAmount став 500.
    invoice["paymentInfo"] = _fake_payment_info(
        rrn_suffix="2", fee_kopecks=_fake_fee_kopecks(amount)
    )
    logging.info(
        "✅ Інвойс %s фіналізовано на %s коп. (було утримано %s коп.)",
        invoice_id, amount, invoice["amount"],
    )
    return {"status": "success"}


@app.post("/api/merchant/invoice/cancel")
async def cancel_invoice(request: Request, x_token: str = Header(None)):
    """
    Скасування — працює і на success (повернення вже списаного), і на
    НЕфіналізованому hold (звільнення утриманого без жодного списання).

    ЖИВИЙ СМОУК 28-29.07.2026: cancel на нефіналізованому hold СПРАЦЮВАВ
    (підтверджено карткою — «Скасування. eVolt UA +20 ₴», без комісії) —
    це і був головний невідомий гейта Моделі B (3c-ii), документація банку
    описує цей метод лише для успішної оплати. Уся форма нижче — ЖИВА.

    СВІДОМЕ СПРОЩЕННЯ МОКА (не властивість банку): документація банку описує
    опційне поле `amount` у тілі запиту для ЧАСТКОВОГО скасування — мок його
    ігнорує й завжди скасовує ПОВНІСТЮ. Для Моделі B поки не потрібне
    (у смоуку 28-29.07.2026 cancel викликався без `amount`, лише на повне
    скасування); додати підтримку, якщо колись знадобиться частковий cancel.
    """
    body = await request.json()
    invoice_id = body.get("invoiceId")

    invoice = _invoices.get(invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    if invoice["_token"] != x_token:
        raise HTTPException(status_code=403, detail="foreign invoice")
    if invoice["status"] not in ("success", "hold"):
        raise HTTPException(
            status_code=400,
            detail=f"cannot cancel invoice in status {invoice['status']}",
        )

    previous_status = invoice["status"]
    cancelled_amount = invoice["finalAmount"] if previous_status == "success" else invoice["amount"]
    now = _now_iso()
    invoice.setdefault("cancelList", []).append({
        "status": "success",
        "amount": cancelled_amount,
        "ccy": invoice.get("ccy") or 980,
        "createdDate": now,
        "modifiedDate": now,
        "approvalCode": "000000",
        "rrn": "000000000003",
    })
    invoice["status"] = "reversed"
    invoice["finalAmount"] = 0
    invoice["modifiedDate"] = now
    logging.info("↩️ Інвойс %s скасовано (був %s, скасовано %s коп.)",
                 invoice_id, previous_status, cancelled_amount)
    return {"status": "success", "createdDate": now, "modifiedDate": now}


@app.get("/mock/page/{invoice_id}")
async def mock_page(invoice_id: str):
    """Заглушка сторінки оплати Monobank."""
    return {
        "info": "Сторінка оплати Monobank (мок)",
        "invoiceId": invoice_id,
        "pay": f"POST /mock/pay/{invoice_id}",
    }
