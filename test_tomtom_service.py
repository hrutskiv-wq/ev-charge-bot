"""
Тести на app/services/tomtom_service.py — живий інфошар TomTom у водійському
пошуку. Юридична вимога (T&C TomTom п. 11.4): жодного кешу, жодного запису
в БД — кожен тест б'є напряму в мокнутий httpx, без побічних сховищ.

Запуск: pytest test_tomtom_service.py -v
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import app.services.tomtom_service as tomtom_service
from app.services.tomtom_service import (
    search_stations_near,
    get_availability,
    is_enabled,
    _normalize_search_result,
    _normalize_availability,
    TOMTOM_DAILY_BUDGET,
)


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
    return mock_client_cm, mock_client


# Форма звірена з живою відповіддю TomTom 31.07.2026: chargingPark — поле
# ВЕРХНЬОГО рівня result (НЕ всередині poi — poi.chargingPark у живій
# відповіді завжди порожній), кожен конектор несе connectorType/ratedPowerKW/
# voltageV/currentA/currentType. Той самий принцип, що mock_monobank.py —
# мок підганяємо під реальність, а не навпаки.
SAMPLE_SEARCH_RESULT = {
    "id": "abc123",
    "dist": 940.0,
    "poi": {
        "name": "Зубра HyperCharger",
    },
    "address": {"freeformAddress": "Зубра, 1, Львів"},
    "position": {"lat": 49.79, "lon": 23.95},
    "chargingPark": {
        "connectors": [
            {
                "connectorType": "IEC62196Type2CCS",
                "ratedPowerKW": 150,
                "voltageV": 500,
                "currentA": 300,
                "currentType": "DC",
            },
            {
                "connectorType": "Chademo",
                "ratedPowerKW": 50,
                "voltageV": 500,
                "currentA": 125,
                "currentType": "DC",
            },
        ]
    },
    "dataSources": {"chargingAvailability": {"id": "avail-xyz"}},
}

SAMPLE_AVAILABILITY = {
    "connectors": [
        {
            "type": "IEC62196Type2CCS",
            "availability": {"current": {"available": "1", "occupied": "0", "outOfService": "0"}},
        },
        {
            "type": "Chademo",
            "availability": {"current": {"available": "0", "occupied": "1", "outOfService": "0"}},
        },
    ]
}


def _reset_budget():
    tomtom_service._budget._day = None
    tomtom_service._budget._count = 0


def setup_function(_):
    _reset_budget()


# ---------------------------------------------------------------------------
# is_enabled / вимкнений без ключа
# ---------------------------------------------------------------------------

async def test_disabled_without_key_returns_none_and_makes_no_http_call():
    with patch.object(tomtom_service, "TOMTOM_API_KEY", None), \
         patch("app.services.tomtom_service.httpx.AsyncClient") as mock_client_cls:
        assert is_enabled() is False
        result = await search_stations_near(50.1, 30.1)

    assert result is None
    mock_client_cls.assert_not_called()


async def test_get_availability_disabled_without_key_returns_none_no_http_call():
    with patch.object(tomtom_service, "TOMTOM_API_KEY", None), \
         patch("app.services.tomtom_service.httpx.AsyncClient") as mock_client_cls:
        result = await get_availability("some-id")

    assert result is None
    mock_client_cls.assert_not_called()


async def test_get_availability_without_id_returns_none_no_http_call():
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient") as mock_client_cls:
        result = await get_availability(None)

    assert result is None
    mock_client_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Нормалізація search
# ---------------------------------------------------------------------------

def test_normalize_search_result_maps_fields_and_picks_strongest_connector():
    normalized = _normalize_search_result(SAMPLE_SEARCH_RESULT)

    assert normalized["id"] == "TOMTOM-abc123"
    assert normalized["name"] == "Зубра HyperCharger"
    assert normalized["lat"] == 49.79
    assert normalized["lon"] == 23.95
    assert normalized["address"] == "Зубра, 1, Львів"
    assert normalized["distance_km"] == 0.94
    assert normalized["power_kw"] == 150
    assert normalized["connector_type"] == "CCS (Type 2)"
    assert normalized["charging_availability_id"] == "avail-xyz"
    assert len(normalized["connectors"]) == 2


def test_normalize_search_result_without_position_returns_none():
    bare = {"id": "no-pos", "poi": {"name": "Без координат"}, "position": {}}
    assert _normalize_search_result(bare) is None


def test_normalize_search_result_reads_chargingpark_from_result_not_poi():
    """Регресія на БЛОКЕР 1 (рев'ю Opus 31.07.2026): TomTom повертає
    chargingPark на ВЕРХньому рівні result, а не всередині poi. Раніше код
    читав poi.chargingPark, який у живій відповіді завжди порожній —
    connectors завжди виходили [], бейджа швидкості не було НІКОЛИ."""
    result = {
        "id": "toplevel",
        "position": {"lat": 1.0, "lon": 2.0},
        "poi": {
            "name": "X",
            # Пастка: якби код і далі читав звідси, тест пройшов би з
            # НЕПРАВИЛЬНИМИ даними — навмисно інше значення потужності.
            "chargingPark": {"connectors": [{"connectorType": "WrongPlace", "ratedPowerKW": 999}]},
        },
        "chargingPark": {"connectors": [{"connectorType": "IEC62196Type2CCS", "ratedPowerKW": 50}]},
    }
    normalized = _normalize_search_result(result)

    assert normalized["power_kw"] == 50
    assert normalized["connector_type"] == "CCS (Type 2)"


def test_normalize_search_result_prefers_dc_connector_when_no_power_data():
    """ПРАВКА 5 (рев'ю Opus): currentType (AC1/AC3/DC) — прямий сигнал
    TomTom, використовується для вибору представницького конектора станції,
    коли жоден конектор не дав ratedPowerKW. classify_station_speed сама не
    змінювалась — це лише кращий вхід для неї."""
    result = {
        "id": "nopower",
        "position": {"lat": 1.0, "lon": 2.0},
        "chargingPark": {
            "connectors": [
                {"connectorType": "SomeUnmappedAC", "currentType": "AC1"},
                {"connectorType": "SomeUnmappedDC", "currentType": "DC"},
            ]
        },
    }
    normalized = _normalize_search_result(result)

    assert normalized["power_kw"] is None
    assert normalized["connector_type"] == "SomeUnmappedDC"
    assert normalized["connectors"][0]["current_type"] == "AC1"
    assert normalized["connectors"][1]["current_type"] == "DC"


def test_normalize_search_result_missing_optional_fields_uses_safe_defaults():
    minimal = {
        "id": "min1",
        "position": {"lat": 1.0, "lon": 2.0},
    }
    normalized = _normalize_search_result(minimal)

    assert normalized["name"] == "Без назви"
    assert normalized["address"] == "Адреса не вказана"
    assert normalized["distance_km"] is None
    assert normalized["power_kw"] is None
    assert normalized["connector_type"] is None
    assert normalized["charging_availability_id"] is None
    assert normalized["connectors"] == []


async def test_search_stations_near_end_to_end_success():
    mock_cm, _ = _make_mock_client(json_data={"results": [SAMPLE_SEARCH_RESULT]})
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        result = await search_stations_near(49.79, 23.95)

    assert result is not None
    assert len(result) == 1
    assert result[0]["id"] == "TOMTOM-abc123"


async def test_search_stations_near_empty_results_returns_empty_list_not_none():
    """Успішна відповідь без станцій у радіусі — це НЕ фолбек-умова."""
    mock_cm, _ = _make_mock_client(json_data={"results": []})
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        result = await search_stations_near(49.79, 23.95)

    assert result == []


# ---------------------------------------------------------------------------
# Нормалізація availability
# ---------------------------------------------------------------------------

def test_normalize_availability_maps_counts_per_connector():
    normalized = _normalize_availability(SAMPLE_AVAILABILITY)

    assert len(normalized) == 2
    ccs = next(c for c in normalized if c["connector_type"] == "CCS (Type 2)")
    assert ccs["available"] == 1
    assert ccs["occupied"] == 0
    assert ccs["out_of_service"] == 0

    chademo = next(c for c in normalized if c["connector_type"] == "CHAdeMO")
    assert chademo["available"] == 0
    assert chademo["occupied"] == 1


def test_normalize_availability_handles_missing_current_block():
    data = {"connectors": [{"type": "Tesla", "availability": {}}]}
    normalized = _normalize_availability(data)
    assert normalized[0]["available"] == 0
    assert normalized[0]["occupied"] == 0
    assert normalized[0]["out_of_service"] == 0


async def test_get_availability_end_to_end_success():
    mock_cm, _ = _make_mock_client(json_data=SAMPLE_AVAILABILITY)
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        result = await get_availability("avail-xyz")

    assert result is not None
    assert len(result) == 2


async def test_get_availability_uses_correct_query_param_name():
    """Регресія на БЛОКЕР 2 (рев'ю Opus 31.07.2026): живий запит показав, що
    параметр зветься "chargingAvailability", а не "chargingAvailabilityId" —
    з "Id" TomTom відповідав 400 Bad Request, і get_availability() ЗАВЖДИ
    мовчки повертав None (лише warning у лог)."""
    mock_cm, mock_client = _make_mock_client(json_data=SAMPLE_AVAILABILITY)
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        await get_availability("avail-xyz")

    called_params = mock_client.get.call_args.kwargs["params"]
    assert called_params.get("chargingAvailability") == "avail-xyz"
    assert "chargingAvailabilityId" not in called_params


# ---------------------------------------------------------------------------
# Запобіжник квоти
# ---------------------------------------------------------------------------

async def test_quota_exhausted_returns_none_without_http_call():
    tomtom_service._budget._day = tomtom_service.datetime.now(tomtom_service.timezone.utc).date()
    tomtom_service._budget._count = TOMTOM_DAILY_BUDGET

    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient") as mock_client_cls:
        result = await search_stations_near(49.79, 23.95)

    assert result is None
    mock_client_cls.assert_not_called()


async def test_quota_exhausted_blocks_availability_too_shared_budget():
    tomtom_service._budget._day = tomtom_service.datetime.now(tomtom_service.timezone.utc).date()
    tomtom_service._budget._count = TOMTOM_DAILY_BUDGET

    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient") as mock_client_cls:
        result = await get_availability("avail-xyz")

    assert result is None
    mock_client_cls.assert_not_called()


async def test_budget_resets_on_new_utc_day():
    import datetime as real_datetime

    yesterday = real_datetime.datetime.now(real_datetime.timezone.utc).date() - real_datetime.timedelta(days=1)
    tomtom_service._budget._day = yesterday
    tomtom_service._budget._count = TOMTOM_DAILY_BUDGET

    mock_cm, _ = _make_mock_client(json_data={"results": []})
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        result = await search_stations_near(49.79, 23.95)

    assert result == []


# ---------------------------------------------------------------------------
# 429 / 5xx / таймаут — фолбек без винятку
# ---------------------------------------------------------------------------

async def test_429_returns_none_without_raising():
    mock_cm, _ = _make_mock_client(status_code=429, json_data=None)
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        result = await search_stations_near(49.79, 23.95)

    assert result is None


async def test_5xx_returns_none_without_raising():
    mock_cm, _ = _make_mock_client(status_code=503, json_data=None)
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        result = await search_stations_near(49.79, 23.95)

    assert result is None


async def test_timeout_returns_none_without_raising():
    mock_cm, _ = _make_mock_client(raise_timeout=True)
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        result = await search_stations_near(49.79, 23.95)

    assert result is None


async def test_availability_5xx_returns_none_without_raising():
    mock_cm, _ = _make_mock_client(status_code=500, json_data=None)
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "fake-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        result = await get_availability("avail-xyz")

    assert result is None


# ---------------------------------------------------------------------------
# Ключ не логується
# ---------------------------------------------------------------------------

async def test_api_key_not_logged_on_http_error(caplog):
    mock_cm, _ = _make_mock_client(status_code=500, json_data=None)
    with patch.object(tomtom_service, "TOMTOM_API_KEY", "super-secret-key"), \
         patch("app.services.tomtom_service.httpx.AsyncClient", return_value=mock_cm):
        await search_stations_near(49.79, 23.95)

    assert "super-secret-key" not in caplog.text
