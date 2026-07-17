"""
Тести на app/services/ocm_service.py::find_three_nearest_stations — пошук
найближчих станцій через Open Charge Map API. Раніше цей модуль взагалі
не мав тестів, хоча активно використовується в проді (кнопка "Зарядка" →
"Надіслати розташування").

Заразом із написанням тестів знайдено і виправлено реальний баг: декоратор
`@cached` кешував РЕЗУЛЬТАТ ПОМИЛКИ (None від таймауту чи 5xx) на ті самі
300 секунд, що й успішний результат. Тобто один тимчасовий збій OCM API
"заморожував" відповідь для цієї локації на 5 хвилин, навіть якщо API
відновлювалось за секунди. Виправлено через `skip_cache_func`.

Кожен тест використовує УНІКАЛЬНІ координати, щоб не ділити кеш
(ключ будується як round(lat, 3):round(lon, 3)) з іншими тестами в тому ж
процесі pytest.

Запуск: pytest test_ocm_service.py -v
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.services.ocm_service import find_three_nearest_stations


def _make_mock_client(status_code=200, json_data=None, raise_timeout=False):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = "error body"
    mock_response.json = MagicMock(return_value=json_data)

    mock_client = AsyncMock()
    if raise_timeout:
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    else:
        mock_client.get = AsyncMock(return_value=mock_response)

    mock_client_cm = AsyncMock()
    mock_client_cm.__aenter__.return_value = mock_client
    mock_client_cm.__aexit__.return_value = None
    return mock_client_cm


SAMPLE_POI = {
    "ID": 12345,
    "AddressInfo": {
        "Title": "Зубра HyperCharger",
        "AddressLine1": "Зубра, 1",
        "Distance": 0.94,
        "Latitude": 49.79,
        "Longitude": 23.95,
    },
    "OperatorInfo": {"Title": "Go ToU"},
    "Connections": [
        {"ConnectionType": {"Title": "CCS (Type 2)"}, "PowerKW": 240, "Quantity": 2},
    ],
}


async def test_successful_response_parses_station_correctly():
    with patch("app.services.ocm_service.httpx.AsyncClient", return_value=_make_mock_client(json_data=[SAMPLE_POI])), \
         patch("app.services.ocm_service.save_station_to_local_db", new=AsyncMock()) as mock_save:
        result = await find_three_nearest_stations(50.111, 30.111)

    assert result is not None
    assert len(result) == 1
    station = result[0]
    assert station["id"] == "OCM-12345"
    assert station["name"] == "Зубра HyperCharger"
    assert station["operator"] == "Go ToU"
    assert "CCS (Type 2) (240 кВт) x2" in station["connectors"]
    mock_save.assert_awaited_once()


async def test_empty_list_returns_empty_not_none():
    """Порожній результат (немає станцій у радіусі) — це не помилка,
    UI має показати "не знайдено", а не "помилка API"."""
    with patch("app.services.ocm_service.httpx.AsyncClient", return_value=_make_mock_client(json_data=[])):
        result = await find_three_nearest_stations(50.222, 30.222)

    assert result == []


async def test_non_200_status_returns_none():
    with patch("app.services.ocm_service.httpx.AsyncClient", return_value=_make_mock_client(status_code=500, json_data=None)):
        result = await find_three_nearest_stations(50.333, 30.333)

    assert result is None


async def test_timeout_returns_none_not_raises():
    with patch("app.services.ocm_service.httpx.AsyncClient", return_value=_make_mock_client(raise_timeout=True)):
        result = await find_three_nearest_stations(50.444, 30.444)

    assert result is None


async def test_missing_operator_and_connections_uses_safe_defaults():
    """POI з неповними даними (без OperatorInfo/Connections) не має
    валити весь пошук — лише показувати безпечні дефолти."""
    bare_poi = {
        "ID": 999,
        "AddressInfo": {"Title": "Станція без деталей", "AddressLine1": "Невідомо"},
    }
    with patch("app.services.ocm_service.httpx.AsyncClient", return_value=_make_mock_client(json_data=[bare_poi])), \
         patch("app.services.ocm_service.save_station_to_local_db", new=AsyncMock()):
        result = await find_three_nearest_stations(50.555, 30.555)

    assert result[0]["operator"] == "Невідомий оператор"
    assert result[0]["connectors"] == "Інформація відсутня"


async def test_error_result_is_not_cached_for_five_minutes():
    """Регресійний тест на знайдений баг: помилка (None) не повинна
    кешуватись — інакше OCM-таймаут/5xx "заморожує" відповідь на 300с
    для цієї локації навіть після відновлення API."""
    error_client = _make_mock_client(status_code=500, json_data=None)
    success_client = _make_mock_client(json_data=[SAMPLE_POI])

    lat, lon = 50.666, 30.666

    with patch("app.services.ocm_service.httpx.AsyncClient", return_value=error_client):
        first_result = await find_three_nearest_stations(lat, lon)
    assert first_result is None

    with patch("app.services.ocm_service.httpx.AsyncClient", return_value=success_client), \
         patch("app.services.ocm_service.save_station_to_local_db", new=AsyncMock()):
        second_result = await find_three_nearest_stations(lat, lon)

    # Якби помилка закешувалась — тут ми б знову отримали None з кешу,
    # а не реальний результат другого (успішного) виклику.
    assert second_result is not None
    assert len(second_result) == 1
