"""
Тести на app/services/ocpp_charging.py — сервісне ядро "резерв + RemoteStart"
/ "RemoteStop" (Промпт 3c-i, бот-адмінкоманди). Той самий фейковий підхід,
що в test_charging_reservations.py: repo.* і remote_start_transaction/
remote_stop_transaction підмінені напряму, жива БД/OCPP не потрібні.

Ключова гарантія, яку перевіряє більшість тестів тут: на КОЖНІЙ гілці
відмови ПІСЛЯ створення резервації (not_ocpp/not_connected/rejected)
release_reservation_hold() викликається РІВНО раз — нетто-вплив на баланс
водія 0 (hold і release компенсують одне одного). Сам факт hold/release —
через update_user_balance() — уже перевірено в test_balance.py й
test_charging_reservations.py; тут перевіряється лише ОРКЕСТРАЦІЯ навколо
нього, плюс паритет CLI (start_charging_session.py) з сервісом після
рефактора.

Група "герметизація hold" (feature/ocpp-hold-hardening) додатково
перевіряє: БУДЬ-ЯКИЙ неочікуваний виняток між hold і RemoteStart (не лише
ChargePointNotConnected) теж звільняє hold рівно раз і перевикидається
назовні незміненим — включно з asyncio.CancelledError і з випадком, коли
саме звільнення теж падає (тоді назовні летить ОРИГІНАЛЬНИЙ виняток, не
помилка release).

Запуск: pytest test_ocpp_charging_service.py -v
"""
import asyncio
from decimal import Decimal

import pytest

from app.api.ocpp_ws import ChargePointNotConnected
from app.database import operators_repo as repo
from app.services import ocpp_charging

OPERATOR_A = 1
STATION_ID = 10
USER_ID = 555
RESERVATION_ID = 42
ID_TAG = "reservation-tag16"
CP_ID = "CP-1"


def _station(ocpp=True):
    return {"id": STATION_ID, "operator_id": OPERATOR_A,
            "ocpp_charge_point_id": CP_ID if ocpp else None}


def _fake_get_station(result):
    """repo.get_station() — async; monkeypatch потребує async-заглушку, не sync lambda."""
    async def _get(*a, **kw):
        return result
    return _get


# ---------------------------------------------------------------------------
# start_charging_session()
# ---------------------------------------------------------------------------

async def test_start_happy_path_returns_ok_and_does_not_release(monkeypatch):
    async def fake_create(*a, **kw):
        return RESERVATION_ID, ID_TAG, None
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    release_calls = []
    async def fake_release(*a, **kw):
        release_calls.append((a, kw))
        return True
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    remote_start_calls = []
    async def fake_remote_start(operator_id, cp_id, id_tag, connector_id=None):
        remote_start_calls.append((operator_id, cp_id, id_tag))
        return True
    monkeypatch.setattr(ocpp_charging, "remote_start_transaction", fake_remote_start)

    result = await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("20.000"))

    assert result.status == "ok"
    assert result.reservation_id == RESERVATION_ID
    assert result.id_tag == ID_TAG
    assert remote_start_calls == [(OPERATOR_A, CP_ID, ID_TAG)]
    assert release_calls == [], "Успіх — hold НЕ звільняється (баланс водія лишається зарезервованим)"


async def test_start_insufficient_balance_does_not_call_remote_start(monkeypatch):
    async def fake_create(*a, **kw):
        return None, None, "insufficient_balance"
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)

    remote_start_calls = []
    async def fake_remote_start(*a, **kw):
        remote_start_calls.append((a, kw))
        return True
    monkeypatch.setattr(ocpp_charging, "remote_start_transaction", fake_remote_start)

    release_calls = []
    async def fake_release(*a, **kw):
        release_calls.append((a, kw))
        return True
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    result = await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("999.000"))

    assert result.status == "insufficient_balance"
    assert result.reservation_id is None
    assert remote_start_calls == [], "Не мало дійти до RemoteStart — резервацію взагалі не створено"
    assert release_calls == [], "Нема чого звільняти — резервації не було"


async def test_start_unknown_station_returns_status_without_touching_remote(monkeypatch):
    async def fake_create(*a, **kw):
        return None, None, "unknown_station"
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)

    result = await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("5.000"))

    assert result.status == "unknown_station"
    assert result.reservation_id is None


async def test_start_not_ocpp_station_releases_hold(monkeypatch):
    async def fake_create(*a, **kw):
        return RESERVATION_ID, ID_TAG, None
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station(ocpp=False)))

    release_calls = []
    async def fake_release(operator_id, reservation_id, new_status):
        release_calls.append((operator_id, reservation_id, new_status))
        return True
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    result = await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("5.000"))

    assert result.status == "not_ocpp"
    assert result.reservation_id == RESERVATION_ID
    assert release_calls == [(OPERATOR_A, RESERVATION_ID, "cancelled")]


async def test_start_not_connected_releases_hold_net_zero_balance_effect(monkeypatch):
    async def fake_create(*a, **kw):
        return RESERVATION_ID, ID_TAG, None
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    release_calls = []
    async def fake_release(operator_id, reservation_id, new_status):
        release_calls.append((operator_id, reservation_id, new_status))
        return True
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    async def fake_remote_start(*a, **kw):
        raise ChargePointNotConnected(CP_ID)
    monkeypatch.setattr(ocpp_charging, "remote_start_transaction", fake_remote_start)

    result = await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("5.000"))

    assert result.status == "not_connected"
    assert result.reservation_id == RESERVATION_ID
    assert release_calls == [(OPERATOR_A, RESERVATION_ID, "cancelled")], (
        "hold і release компенсують одне одного — нетто-вплив на баланс водія 0"
    )


async def test_start_rejected_releases_hold(monkeypatch):
    async def fake_create(*a, **kw):
        return RESERVATION_ID, ID_TAG, None
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    release_calls = []
    async def fake_release(operator_id, reservation_id, new_status):
        release_calls.append((operator_id, reservation_id, new_status))
        return True
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    async def fake_remote_start(*a, **kw):
        return False
    monkeypatch.setattr(ocpp_charging, "remote_start_transaction", fake_remote_start)

    result = await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("5.000"))

    assert result.status == "rejected"
    assert release_calls == [(OPERATOR_A, RESERVATION_ID, "cancelled")]


# ---------------------------------------------------------------------------
# Герметизація hold — неочікувані винятки між hold і RemoteStart
# (feature/ocpp-hold-hardening)
# ---------------------------------------------------------------------------

async def test_start_get_station_unexpected_error_releases_hold_and_reraises(monkeypatch):
    async def fake_create(*a, **kw):
        return RESERVATION_ID, ID_TAG, None
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)

    async def fake_get_station(*a, **kw):
        raise RuntimeError("db connection lost")
    monkeypatch.setattr(repo, "get_station", fake_get_station)

    release_calls = []
    async def fake_release(operator_id, reservation_id, new_status):
        release_calls.append((operator_id, reservation_id, new_status))
        return True
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    with pytest.raises(RuntimeError, match="db connection lost"):
        await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("5.000"))

    assert release_calls == [(OPERATOR_A, RESERVATION_ID, "cancelled")]


async def test_start_remote_start_unexpected_error_releases_hold_and_reraises(monkeypatch):
    async def fake_create(*a, **kw):
        return RESERVATION_ID, ID_TAG, None
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    async def fake_remote_start(*a, **kw):
        raise TimeoutError("station handshake timed out")
    monkeypatch.setattr(ocpp_charging, "remote_start_transaction", fake_remote_start)

    release_calls = []
    async def fake_release(operator_id, reservation_id, new_status):
        release_calls.append((operator_id, reservation_id, new_status))
        return True
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    with pytest.raises(TimeoutError, match="station handshake timed out"):
        await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("5.000"))

    assert release_calls == [(OPERATOR_A, RESERVATION_ID, "cancelled")]


async def test_start_cancelled_error_releases_hold_and_reraises(monkeypatch):
    """
    asyncio.CancelledError успадковується від BaseException, не Exception —
    рестарт контейнера/скасування задачі рівно між hold і RemoteStart мусить
    так само звільняти hold, а не лише "звичайні" винятки.
    """
    async def fake_create(*a, **kw):
        return RESERVATION_ID, ID_TAG, None
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    async def fake_remote_start(*a, **kw):
        raise asyncio.CancelledError()
    monkeypatch.setattr(ocpp_charging, "remote_start_transaction", fake_remote_start)

    release_calls = []
    async def fake_release(operator_id, reservation_id, new_status):
        release_calls.append((operator_id, reservation_id, new_status))
        return True
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    with pytest.raises(asyncio.CancelledError):
        await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("5.000"))

    assert release_calls == [(OPERATOR_A, RESERVATION_ID, "cancelled")]


async def test_start_real_double_cancel_release_still_completes_under_shield(monkeypatch):
    """
    Тест вище (`test_start_cancelled_error_releases_hold_and_reraises`)
    синтетичний — `raise asyncio.CancelledError()` з мока не скасовує
    жодну реальну задачу, тож asyncio.shield() узагалі не залучається.
    Цей тест скасовує СПРАВЖНЮ задачу ДВІЧІ поспіль — сценарій, який
    насправді потребує shield (рев'ю: одиночний cancel добігає release і
    без shield, бо прапорець скасування знімається одразу після першого
    кидка; лише ПОВТОРНИЙ cancel на тій самій задачі обірвав би плейн
    await, поки release ще не завершився):

    1-й cancel ловиться на await remote_start_transaction() (зовнішній
    except BaseException) і стартує release під shield; 2-й cancel
    прилітає на зовнішній await(shield) саме тоді, коли release-корутина
    ще виконується у фоні (await asyncio.sleep всередині неї) — без
    shield цей другий cancel обірвав би й саму release-корутину.
    """
    async def fake_create(*a, **kw):
        return RESERVATION_ID, ID_TAG, None
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    remote_start_entered = asyncio.Event()
    block_remote_start = asyncio.Event()

    async def fake_remote_start(*a, **kw):
        remote_start_entered.set()
        await block_remote_start.wait()
        return True  # ніколи не досягається — задачу скасують раніше
    monkeypatch.setattr(ocpp_charging, "remote_start_transaction", fake_remote_start)

    release_entered = asyncio.Event()
    release_calls = []
    async def fake_release(operator_id, reservation_id, new_status):
        release_entered.set()
        await asyncio.sleep(0.05)  # тримає release "у польоті" для 2-го cancel
        release_calls.append((operator_id, reservation_id, new_status))
        return True
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    task = asyncio.create_task(
        ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("5.000"))
    )
    await remote_start_entered.wait()

    task.cancel()  # 1-й cancel — ловиться на await remote_start_transaction()
    await release_entered.wait()  # except BaseException вже стартував release під shield

    task.cancel()  # 2-й cancel — влучає в зовнішній await(shield), НЕ в саму release-корутину

    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.1)  # дати фоновій release-корутині (під shield) дожити до кінця

    assert release_calls == [(OPERATOR_A, RESERVATION_ID, "cancelled")], (
        "Подвійне скасування задачі: release під asyncio.shield() мав дожити "
        "до кінця у фоні, навіть коли зовнішній await на shield теж скасовано"
    )


async def test_start_release_itself_fails_original_exception_still_wins(monkeypatch):
    """
    Якщо і основна операція впала, і компенсуюче звільнення теж впало —
    назовні має вилетіти ОРИГІНАЛЬНИЙ виняток (діагностика первинної
    причини важливіша за помилку компенсації), а не помилку release.
    """
    async def fake_create(*a, **kw):
        return RESERVATION_ID, ID_TAG, None
    monkeypatch.setattr(repo, "create_charging_reservation", fake_create)

    async def fake_get_station(*a, **kw):
        raise ValueError("original")
    monkeypatch.setattr(repo, "get_station", fake_get_station)

    async def fake_release(*a, **kw):
        raise RuntimeError("release failed")
    monkeypatch.setattr(repo, "release_reservation_hold", fake_release)

    with pytest.raises(ValueError, match="original"):
        await ocpp_charging.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("5.000"))


# ---------------------------------------------------------------------------
# stop_charging_session()
# ---------------------------------------------------------------------------

async def test_stop_happy_path_calls_remote_stop_with_active_transaction_id(monkeypatch):
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    async def fake_get_active(operator_id, station_id):
        return {"ocpp_transaction_id": 777}
    monkeypatch.setattr(repo, "get_active_ocpp_session", fake_get_active)

    remote_stop_calls = []
    async def fake_remote_stop(operator_id, cp_id, transaction_id):
        remote_stop_calls.append((operator_id, cp_id, transaction_id))
        return True
    monkeypatch.setattr(ocpp_charging, "remote_stop_transaction", fake_remote_stop)

    result = await ocpp_charging.stop_charging_session(OPERATOR_A, STATION_ID)

    assert result.status == "ok"
    assert result.transaction_id == 777
    assert remote_stop_calls == [(OPERATOR_A, CP_ID, 777)]


async def test_stop_no_active_session_returns_status_without_remote_call(monkeypatch):
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    async def fake_get_active(*a, **kw):
        return None
    monkeypatch.setattr(repo, "get_active_ocpp_session", fake_get_active)

    remote_stop_calls = []
    async def fake_remote_stop(*a, **kw):
        remote_stop_calls.append((a, kw))
        return True
    monkeypatch.setattr(ocpp_charging, "remote_stop_transaction", fake_remote_stop)

    result = await ocpp_charging.stop_charging_session(OPERATOR_A, STATION_ID)

    assert result.status == "no_active_session"
    assert remote_stop_calls == []


async def test_stop_unknown_station_returns_status(monkeypatch):
    monkeypatch.setattr(repo, "get_station", _fake_get_station(None))

    result = await ocpp_charging.stop_charging_session(OPERATOR_A, STATION_ID)

    assert result.status == "unknown_station"


async def test_stop_not_connected(monkeypatch):
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    async def fake_get_active(*a, **kw):
        return {"ocpp_transaction_id": 777}
    monkeypatch.setattr(repo, "get_active_ocpp_session", fake_get_active)

    async def fake_remote_stop(*a, **kw):
        raise ChargePointNotConnected(CP_ID)
    monkeypatch.setattr(ocpp_charging, "remote_stop_transaction", fake_remote_stop)

    result = await ocpp_charging.stop_charging_session(OPERATOR_A, STATION_ID)

    assert result.status == "not_connected"
    assert result.transaction_id == 777


async def test_stop_rejected(monkeypatch):
    monkeypatch.setattr(repo, "get_station", _fake_get_station(_station()))

    async def fake_get_active(*a, **kw):
        return {"ocpp_transaction_id": 777}
    monkeypatch.setattr(repo, "get_active_ocpp_session", fake_get_active)

    async def fake_remote_stop(*a, **kw):
        return False
    monkeypatch.setattr(ocpp_charging, "remote_stop_transaction", fake_remote_stop)

    result = await ocpp_charging.stop_charging_session(OPERATOR_A, STATION_ID)

    assert result.status == "rejected"


# ---------------------------------------------------------------------------
# Паритет CLI <-> сервіс (start_charging_session.py — тонка обгортка)
# ---------------------------------------------------------------------------

async def test_cli_wrapper_delegates_to_service_and_preserves_return_contract(monkeypatch, capsys):
    import start_charging_session as cli

    async def fake_service_start(operator_id, station_id, user_id, reserved_kwh):
        assert (operator_id, station_id, user_id, reserved_kwh) == (
            OPERATOR_A, STATION_ID, USER_ID, Decimal("20.000"),
        )
        return ocpp_charging.ChargingStartResult(status="ok", reservation_id=RESERVATION_ID, id_tag=ID_TAG)
    monkeypatch.setattr(cli, "_start_charging_session", fake_service_start)

    reservation_id, id_tag = await cli.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("20.000"))

    assert (reservation_id, id_tag) == (RESERVATION_ID, ID_TAG)
    assert f"#{RESERVATION_ID}" in capsys.readouterr().out


async def test_cli_wrapper_returns_none_none_on_any_failure_status(monkeypatch):
    import start_charging_session as cli

    async def fake_service_start(*a, **kw):
        return ocpp_charging.ChargingStartResult(status="not_connected", reservation_id=RESERVATION_ID, id_tag=ID_TAG)
    monkeypatch.setattr(cli, "_start_charging_session", fake_service_start)

    result = await cli.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("20.000"))

    assert result == (None, None)


async def test_cli_wrapper_propagates_unexpected_exception_from_service(monkeypatch):
    """
    Паритет із сервісом після герметизації hold: CLI-обгортка нічого не
    ловить сама (немає try/except у start_charging_session() тут) — тож
    неочікуваний виняток із сервісу (уже після власного release) проходить
    назовні незміненим і в CLI-шляху так само, не лише в бот-хендлерах.
    """
    import start_charging_session as cli

    async def fake_service_start(*a, **kw):
        raise RuntimeError("db connection lost")
    monkeypatch.setattr(cli, "_start_charging_session", fake_service_start)

    with pytest.raises(RuntimeError, match="db connection lost"):
        await cli.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("20.000"))


async def test_cli_wrapper_handles_unknown_status_gracefully(monkeypatch, capsys):
    """Захист від майбутнього неспівпадіння enum статусів між сервісом і CLI-словником повідомлень."""
    import start_charging_session as cli

    async def fake_service_start(*a, **kw):
        return ocpp_charging.ChargingStartResult(status="some_future_status")
    monkeypatch.setattr(cli, "_start_charging_session", fake_service_start)

    result = await cli.start_charging_session(OPERATOR_A, STATION_ID, USER_ID, Decimal("20.000"))

    assert result == (None, None)
    assert "some_future_status" in capsys.readouterr().out
