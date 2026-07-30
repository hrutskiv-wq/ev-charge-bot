"""
Тести на mock_monobank.py — hold/finalize/cancel флоу (гейт Моделі B, 3c-ii),
навчений формам живого смоуку 28-29.07.2026 (див. докстрінг модуля).

TestClient напряму на FastAPI app мока, без живої мережі — у стилі
test_health.py. `_invoices` — модульний стан у пам'яті, тому кожен тест
створює власний інвойс через /api/merchant/invoice/create (ізоляція між
тестами без явного очищення).

Запуск: pytest test_mock_monobank.py -v
"""
from fastapi.testclient import TestClient

import mock_monobank

client = TestClient(mock_monobank.app)

TOKEN = "merchant-token-abc123"


def _create_invoice(amount=2000, payment_type="hold"):
    resp = client.post(
        "/api/merchant/invoice/create",
        headers={"X-Token": TOKEN},
        json={
            "amount": amount,
            "ccy": 980,
            "paymentType": payment_type,
            "merchantPaymInfo": {"reference": "ref-1", "destination": "Тест"},
        },
    )
    assert resp.status_code == 200
    return resp.json()["invoiceId"]


def _status(invoice_id):
    resp = client.get(
        "/api/merchant/invoice/status",
        params={"invoiceId": invoice_id},
        headers={"X-Token": TOKEN},
    )
    assert resp.status_code == 200
    return resp.json()


def test_hold_invoice_has_no_final_amount_field():
    invoice_id = _create_invoice(amount=2000, payment_type="hold")
    client.post(f"/mock/pay/{invoice_id}")

    body = _status(invoice_id)
    assert body["status"] == "hold"
    assert "finalAmount" not in body
    assert body["amount"] == 2000
    assert body["paymentInfo"]["fee"] > 0


def test_debit_invoice_unaffected_by_hold_logic():
    """paymentType не заданий (звичайний дебет) — поведінка не змінилась."""
    invoice_id = _create_invoice(amount=1000, payment_type="debit")
    client.post(f"/mock/pay/{invoice_id}")

    body = _status(invoice_id)
    assert body["status"] == "success"
    assert body["finalAmount"] == 1000
    assert body["amount"] == 1000


def test_hold_then_partial_finalize_becomes_success_with_final_amount():
    invoice_id = _create_invoice(amount=2000, payment_type="hold")
    client.post(f"/mock/pay/{invoice_id}")

    resp = client.post(
        "/api/merchant/invoice/finalize",
        headers={"X-Token": TOKEN},
        json={"invoiceId": invoice_id, "amount": 500},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "success"}

    body = _status(invoice_id)
    assert body["status"] == "success"
    assert body["amount"] == 2000  # лишається утриманням, не перезаписується
    assert body["finalAmount"] == 500  # фактично списане
    assert body["paymentInfo"]["fee"] == 7  # 500 * 1.3% округлено, як у смоуку


def test_finalize_fee_recomputed_from_final_amount_not_held_amount():
    invoice_id = _create_invoice(amount=2000, payment_type="hold")
    hold_body = client.post(f"/mock/pay/{invoice_id}").json()
    assert hold_body["status"] == "hold"
    hold_fee = _status(invoice_id)["paymentInfo"]["fee"]
    assert hold_fee == 26  # 2000 * 1.3% округлено, як у смоуку

    client.post(
        "/api/merchant/invoice/finalize",
        headers={"X-Token": TOKEN},
        json={"invoiceId": invoice_id, "amount": 500},
    )
    final_fee = _status(invoice_id)["paymentInfo"]["fee"]
    assert final_fee != hold_fee
    assert final_fee == 7


def test_finalize_rejects_amount_greater_than_held():
    """
    Мок повторює ПІДТВЕРДЖЕНУ ЖИВИМ СМОУКОМ 30.07.2026 поведінку банку:
    over-capture (finalize понад hold) банк дійсно відхиляє — HTTP 400,
    errCode "1001", errText "finalization amount exceeds hold amount".
    """
    invoice_id = _create_invoice(amount=2000, payment_type="hold")
    client.post(f"/mock/pay/{invoice_id}")

    resp = client.post(
        "/api/merchant/invoice/finalize",
        headers={"X-Token": TOKEN},
        json={"invoiceId": invoice_id, "amount": 2001},
    )
    assert resp.status_code == 400


def test_finalize_rejects_non_hold_invoice():
    invoice_id = _create_invoice(amount=1000, payment_type="debit")
    client.post(f"/mock/pay/{invoice_id}")  # -> success, не hold

    resp = client.post(
        "/api/merchant/invoice/finalize",
        headers={"X-Token": TOKEN},
        json={"invoiceId": invoice_id, "amount": 500},
    )
    assert resp.status_code == 400


def test_hold_then_cancel_without_finalize_becomes_reversed():
    """Головний невідомий гейта Моделі B: cancel на НЕфіналізованому hold."""
    invoice_id = _create_invoice(amount=2000, payment_type="hold")
    client.post(f"/mock/pay/{invoice_id}")

    resp = client.post(
        "/api/merchant/invoice/cancel",
        headers={"X-Token": TOKEN},
        json={"invoiceId": invoice_id},
    )
    assert resp.status_code == 200
    cancel_body = resp.json()
    assert cancel_body["status"] == "success"
    assert "createdDate" in cancel_body
    assert "modifiedDate" in cancel_body

    body = _status(invoice_id)
    assert body["status"] == "reversed"
    assert body["finalAmount"] == 0
    assert len(body["cancelList"]) == 1
    assert body["cancelList"][0]["amount"] == 2000
    assert body["cancelList"][0]["status"] == "success"


def test_cancel_after_finalize_still_works_on_success():
    invoice_id = _create_invoice(amount=2000, payment_type="hold")
    client.post(f"/mock/pay/{invoice_id}")
    client.post(
        "/api/merchant/invoice/finalize",
        headers={"X-Token": TOKEN},
        json={"invoiceId": invoice_id, "amount": 500},
    )

    resp = client.post(
        "/api/merchant/invoice/cancel",
        headers={"X-Token": TOKEN},
        json={"invoiceId": invoice_id},
    )
    assert resp.status_code == 200

    body = _status(invoice_id)
    assert body["status"] == "reversed"
    assert body["finalAmount"] == 0
    assert body["cancelList"][0]["amount"] == 500  # скасовано фактично списане, не утримане


def test_cancel_rejects_already_reversed_invoice():
    invoice_id = _create_invoice(amount=2000, payment_type="hold")
    client.post(f"/mock/pay/{invoice_id}")
    client.post(
        "/api/merchant/invoice/cancel",
        headers={"X-Token": TOKEN},
        json={"invoiceId": invoice_id},
    )

    resp = client.post(
        "/api/merchant/invoice/cancel",
        headers={"X-Token": TOKEN},
        json={"invoiceId": invoice_id},
    )
    assert resp.status_code == 400


def test_cancel_foreign_invoice_rejected():
    invoice_id = _create_invoice(amount=2000, payment_type="hold")
    client.post(f"/mock/pay/{invoice_id}")

    resp = client.post(
        "/api/merchant/invoice/cancel",
        headers={"X-Token": "someone-elses-token"},
        json={"invoiceId": invoice_id},
    )
    assert resp.status_code == 403


def test_finalize_unknown_invoice_returns_404():
    resp = client.post(
        "/api/merchant/invoice/finalize",
        headers={"X-Token": TOKEN},
        json={"invoiceId": "does-not-exist", "amount": 500},
    )
    assert resp.status_code == 404


def test_cancel_unknown_invoice_returns_404():
    resp = client.post(
        "/api/merchant/invoice/cancel",
        headers={"X-Token": TOKEN},
        json={"invoiceId": "does-not-exist"},
    )
    assert resp.status_code == 404
