"""
Тести на app/services/monobank_acquiring.py — додатки Моделі B (Промпт
3c-ii): paymentType у create_invoice(), finalize_invoice(), cancel_invoice().

httpx.AsyncClient підмінюється через httpx.MockTransport (той самий підхід,
що test_monobank_verify_token.py) — без живої мережі й без mock_monobank.py.

Запуск: pytest test_monobank_acquiring_hold.py -v
"""
import httpx
import pytest

from app.services import monobank_acquiring

TOKEN = "merchant-token-abc123"

_RealAsyncClient = httpx.AsyncClient


def _patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def _client_factory(*args, **kwargs):
        return _RealAsyncClient(transport=transport)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)


# ---------------------------------------------------------------------------
# create_invoice(payment_type=...)
# ---------------------------------------------------------------------------

async def test_create_invoice_debit_payload_unchanged_no_payment_type_field(monkeypatch):
    """
    Замовчування "debit" НЕ додає paymentType у тіло взагалі — наявний
    driver_qr.py флоу лишається байт-в-байт тим самим payload, що й до
    цієї зміни.
    """
    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"invoiceId": "inv-1", "pageUrl": "https://pay.monobank.ua/x"})

    _patch_transport(monkeypatch, handler)

    await monobank_acquiring.create_invoice(
        TOKEN, amount_uah="20", reference="ref-1",
        redirect_url="https://evolt.ua/r", webhook_url="https://evolt.ua/w",
    )

    assert "paymentType" not in captured["body"]


async def test_create_invoice_hold_adds_payment_type_field(monkeypatch):
    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"invoiceId": "inv-2", "pageUrl": "https://pay.monobank.ua/y"})

    _patch_transport(monkeypatch, handler)

    await monobank_acquiring.create_invoice(
        TOKEN, amount_uah="100", reference="ref-2",
        redirect_url="https://evolt.ua/r", webhook_url="https://evolt.ua/w",
        payment_type="hold",
    )

    assert captured["body"]["paymentType"] == "hold"
    assert captured["body"]["amount"] == 10000


# ---------------------------------------------------------------------------
# finalize_invoice()
# ---------------------------------------------------------------------------

async def test_finalize_invoice_sends_invoice_id_and_amount_in_kopecks(monkeypatch):
    captured = {}

    def handler(request):
        import json
        assert request.url.path == monobank_acquiring.FINALIZE_INVOICE_PATH
        assert request.headers["x-token"] == TOKEN
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "success"})

    _patch_transport(monkeypatch, handler)

    result = await monobank_acquiring.finalize_invoice(TOKEN, "inv-3", "5.00")

    assert captured["body"] == {"invoiceId": "inv-3", "amount": 500}
    assert result == {"status": "success"}


async def test_finalize_invoice_raises_on_bank_rejection(monkeypatch):
    def handler(request):
        return httpx.Response(400, text="invoice status is success, expected hold")

    _patch_transport(monkeypatch, handler)

    with pytest.raises(monobank_acquiring.MonobankError):
        await monobank_acquiring.finalize_invoice(TOKEN, "inv-4", "5.00")


async def test_finalize_invoice_raises_on_network_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)

    with pytest.raises(monobank_acquiring.MonobankError):
        await monobank_acquiring.finalize_invoice(TOKEN, "inv-5", "5.00")


# ---------------------------------------------------------------------------
# cancel_invoice()
# ---------------------------------------------------------------------------

async def test_cancel_invoice_sends_only_invoice_id(monkeypatch):
    captured = {}

    def handler(request):
        import json
        assert request.url.path == monobank_acquiring.CANCEL_INVOICE_PATH
        assert request.headers["x-token"] == TOKEN
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "success", "createdDate": "x", "modifiedDate": "y"})

    _patch_transport(monkeypatch, handler)

    result = await monobank_acquiring.cancel_invoice(TOKEN, "inv-6")

    assert captured["body"] == {"invoiceId": "inv-6"}
    assert result["status"] == "success"


async def test_cancel_invoice_raises_on_bank_rejection(monkeypatch):
    def handler(request):
        return httpx.Response(400, text="cannot cancel invoice in status expired")

    _patch_transport(monkeypatch, handler)

    with pytest.raises(monobank_acquiring.MonobankError):
        await monobank_acquiring.cancel_invoice(TOKEN, "inv-7")


async def test_cancel_invoice_raises_on_network_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)

    with pytest.raises(monobank_acquiring.MonobankError):
        await monobank_acquiring.cancel_invoice(TOKEN, "inv-8")
