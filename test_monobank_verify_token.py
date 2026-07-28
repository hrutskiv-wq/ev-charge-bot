"""
Тести на app/services/monobank_acquiring.py::verify_merchant_token() —
верифікація токена оператора реальним зверненням до банку (GET
/api/merchant/details), ключовий критерій #2 самообслуговуваного
онбордингу.

httpx.AsyncClient підмінюється через httpx.MockTransport (без живої мережі,
без mock_monobank.py) — перехоплює запит на рівні транспорту, тож фактичний
BASE_URL не має бути доступним. Три результати чітко розрізнені:
HTTP 200 -> True; HTTP 401/403 (банк явно відхилив токен) -> False;
будь-що інше (5xx, мережевий збій) -> MonobankError.

Запуск: pytest test_monobank_verify_token.py -v
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


async def test_verify_merchant_token_true_on_200(monkeypatch):
    def handler(request):
        assert request.headers["x-token"] == TOKEN
        assert request.url.path == monobank_acquiring.MERCHANT_DETAILS_PATH
        return httpx.Response(200, json={"merchantId": "m1", "merchantName": "Тест ТОВ"})

    _patch_transport(monkeypatch, handler)

    assert await monobank_acquiring.verify_merchant_token(TOKEN) is True


@pytest.mark.parametrize("status_code", [401, 403])
async def test_verify_merchant_token_false_on_explicit_rejection(monkeypatch, status_code):
    def handler(request):
        return httpx.Response(status_code, json={"errorDescription": "invalid token"})

    _patch_transport(monkeypatch, handler)

    assert await monobank_acquiring.verify_merchant_token(TOKEN) is False


async def test_verify_merchant_token_raises_on_unexpected_status(monkeypatch):
    """5xx — банк відповів, але незрозуміло, чи токен поганий, чи сервер упав. Не False, а MonobankError."""
    def handler(request):
        return httpx.Response(500, text="internal error")

    _patch_transport(monkeypatch, handler)

    with pytest.raises(monobank_acquiring.MonobankError):
        await monobank_acquiring.verify_merchant_token(TOKEN)


async def test_verify_merchant_token_raises_on_network_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)

    with pytest.raises(monobank_acquiring.MonobankError):
        await monobank_acquiring.verify_merchant_token(TOKEN)
