"""
Тести на app/database/operators_repo.py — резервації kWh-балансу
(Промпт 3c-i, модель A: резерв -> факт -> звільнення).

Два атомарні складені запити — головний ризик цього бандла:
  * create_charging_reservation() — INSERT резервації + update_user_
    balance(t_type='hold') в ОДНІЙ транзакції: недостатньо балансу ->
    ОБИДВА кроки відкочуються.
  * complete_ocpp_transaction_and_release() — завершення OCPP-сесії +
    update_user_balance(t_type='release') + фіналізація резервації в
    ОДНІЙ транзакції: крах/ретрай між кроками не має лишати застряглий
    'active' резерв (той самий клас бага, що блокер #1 wallet-realmono,
    PR #22).

update_user_balance() підмінюється фейком напряму (керована success/
failure-поведінка) — сама її SQL-логіка hold/release вже покрита
test_balance.py, тут перевіряється лише ОРКЕСТРАЦІЯ навколо неї.

Фейкові asyncpg-з'єднання, той самий підхід, що в test_operator_isolation.py.

Запуск: pytest test_charging_reservations.py -v
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.database import operators_repo as repo

OPERATOR_A = 1
STATION_ID = 10
USER_ID = 555
RESERVATION_ID = 42
SINCE = datetime(2026, 7, 24, tzinfo=timezone.utc)


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


# ---------------------------------------------------------------------------
# create_charging_reservation()
# ---------------------------------------------------------------------------

class FakeReservationInsertConn:
    def __init__(self, reservation_id=RESERVATION_ID):
        self.calls = []
        self.reservation_id = reservation_id

    def _record(self, query, args):
        self.calls.append((" ".join(query.split()), args))

    async def fetchval(self, query, *args):
        self._record(query, args)
        return self.reservation_id

    def transaction(self):
        return _FakeTxn()


@pytest.fixture
def reservation_conn(monkeypatch):
    conn = FakeReservationInsertConn()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)
    return conn


async def test_create_charging_reservation_places_hold_and_returns_id_tag(reservation_conn, monkeypatch):
    held_calls = []

    async def fake_update_user_balance(**kwargs):
        held_calls.append(kwargs)
        return True

    monkeypatch.setattr(repo, "update_user_balance", fake_update_user_balance)

    reservation_id, id_tag, error = await repo.create_charging_reservation(
        OPERATOR_A, STATION_ID, USER_ID, Decimal("20.000"),
    )

    assert reservation_id == RESERVATION_ID
    assert error is None
    assert len(id_tag) == 16, "secrets.token_urlsafe(12) -> 16 символів"

    assert len(held_calls) == 1
    call = held_calls[0]
    assert call["user_id"] == USER_ID
    assert call["amount_kwh"] == Decimal("20.000")
    assert call["t_type"] == "hold"
    assert call["conn"] is reservation_conn, "hold має йти в ТІЙ САМІЙ транзакції, що й INSERT"
    assert call["session_id"] == f"reservation-{RESERVATION_ID}"


async def test_create_charging_reservation_rolls_back_when_balance_insufficient(reservation_conn, monkeypatch):
    async def fake_update_user_balance(**kwargs):
        return False  # недостатньо балансу

    monkeypatch.setattr(repo, "update_user_balance", fake_update_user_balance)

    reservation_id, id_tag, error = await repo.create_charging_reservation(
        OPERATOR_A, STATION_ID, USER_ID, Decimal("999.000"),
    )

    assert (reservation_id, id_tag) == (None, None)
    assert error == "insufficient_balance"


async def test_create_charging_reservation_on_foreign_station_returns_none(monkeypatch):
    conn = FakeReservationInsertConn(reservation_id=None)  # SELECT з operator_stations нічого не знайшов

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    called = []

    async def fake_update_user_balance(**kwargs):
        called.append(kwargs)
        return True

    monkeypatch.setattr(repo, "update_user_balance", fake_update_user_balance)

    reservation_id, id_tag, error = await repo.create_charging_reservation(
        OPERATOR_A, STATION_ID, USER_ID, Decimal("20.000"),
    )

    assert (reservation_id, id_tag) == (None, None)
    assert error == "unknown_station"
    assert called == [], "Не мало дійти до update_user_balance — станція не належить оператору"


async def test_create_charging_reservation_id_tags_are_unique_enough(reservation_conn, monkeypatch):
    """Не криптографічний тест — лише перевірка, що генератор не повертає константу."""
    async def fake_update_user_balance(**kwargs):
        return True

    monkeypatch.setattr(repo, "update_user_balance", fake_update_user_balance)

    _, id_tag_1, _ = await repo.create_charging_reservation(OPERATOR_A, STATION_ID, USER_ID, Decimal("1.000"))
    _, id_tag_2, _ = await repo.create_charging_reservation(OPERATOR_A, STATION_ID, USER_ID, Decimal("1.000"))
    assert id_tag_1 != id_tag_2


# ---------------------------------------------------------------------------
# complete_ocpp_transaction_and_release()
# ---------------------------------------------------------------------------

class FakeReleaseConn:
    def __init__(self, execute_result="UPDATE 1"):
        self.calls = []
        self._execute_result = execute_result

    def _record(self, query, args):
        self.calls.append((" ".join(query.split()), args))

    async def execute(self, query, *args):
        self._record(query, args)
        return self._execute_result

    def transaction(self):
        return _FakeTxn()


@pytest.fixture
def release_conn(monkeypatch):
    conn = FakeReleaseConn()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)
    return conn


@pytest.fixture
def fake_release_balance(monkeypatch):
    released = []

    async def fake_update_user_balance(**kwargs):
        released.append(kwargs)
        return True

    monkeypatch.setattr(repo, "update_user_balance", fake_update_user_balance)
    return released


async def test_complete_and_release_credits_the_unused_remainder(release_conn, fake_release_balance):
    result = await repo.complete_ocpp_transaction_and_release(
        OPERATOR_A, transaction_id=555, kwh=Decimal("15.000"), meter_stop_wh=16000,
        ended_at=SINCE, reservation_id=RESERVATION_ID, reserved_kwh=Decimal("20.000"),
        user_id=USER_ID,
    )

    assert result is True
    assert len(fake_release_balance) == 1
    call = fake_release_balance[0]
    assert call["t_type"] == "release"
    assert call["amount_kwh"] == Decimal("5.000")  # 20.000 - 15.000
    assert call["session_id"] == f"reservation-{RESERVATION_ID}"
    assert call["conn"] is release_conn, "release має йти в ТІЙ САМІЙ транзакції, що completion сесії"

    # Резервація позначена 'finalized' у тій самій транзакції.
    status_query, status_args = release_conn.calls[-1]
    assert "charging_reservations" in status_query
    assert status_args == (OPERATOR_A, RESERVATION_ID, "finalized")


async def test_complete_and_release_with_none_kwh_releases_full_reservation(release_conn, fake_release_balance):
    """Абсурдна дельта лічильника (3b) -> kwh=None -> звільняємо ВЕСЬ резерв, не вигадуємо."""
    await repo.complete_ocpp_transaction_and_release(
        OPERATOR_A, transaction_id=555, kwh=None, meter_stop_wh=16000,
        ended_at=SINCE, reservation_id=RESERVATION_ID, reserved_kwh=Decimal("20.000"),
        user_id=USER_ID,
    )

    assert fake_release_balance[0]["amount_kwh"] == Decimal("20.000")


async def test_complete_and_release_overrun_releases_nothing_and_logs(release_conn, fake_release_balance, caplog):
    """Спожито більше, ніж зарезервовано — нічого звільняти, гучний ERROR-лог на ручний розбір."""
    with caplog.at_level("ERROR", logger="app.database.operators_repo"):
        result = await repo.complete_ocpp_transaction_and_release(
            OPERATOR_A, transaction_id=555, kwh=Decimal("25.000"), meter_stop_wh=26000,
            ended_at=SINCE, reservation_id=RESERVATION_ID, reserved_kwh=Decimal("20.000"),
            user_id=USER_ID,
        )

    assert result is True
    assert fake_release_balance == [], "Перевитрата — звільняти нічого"
    assert "ручний розбір" in caplog.text


async def test_complete_and_release_is_idempotent_on_retry(monkeypatch, fake_release_balance):
    """
    Ретрай StopTransaction (сесія вже 'completed') — мʼютекс усередині
    complete_ocpp_transaction() не пропускає, і ФУНКЦІЯ ЗУПИНЯЄТЬСЯ одразу:
    ні release, ні повторна фіналізація резервації не відбуваються.
    """
    conn = FakeReleaseConn(execute_result="UPDATE 0")

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    result = await repo.complete_ocpp_transaction_and_release(
        OPERATOR_A, transaction_id=555, kwh=Decimal("15.000"), meter_stop_wh=16000,
        ended_at=SINCE, reservation_id=RESERVATION_ID, reserved_kwh=Decimal("20.000"),
        user_id=USER_ID,
    )

    assert result is False
    assert fake_release_balance == [], "Ретрай не мав звільняти залишок вдруге"
    assert len(conn.calls) == 1, "Мав зупинитись одразу після невдалого мʼютексу completion сесії"


# ---------------------------------------------------------------------------
# release_reservation_hold()
# ---------------------------------------------------------------------------

class FakeHoldReleaseConn:
    def __init__(self, fetchrow_result):
        self.calls = []
        self._fetchrow_result = fetchrow_result

    def _record(self, query, args):
        self.calls.append((" ".join(query.split()), args))

    async def fetchrow(self, query, *args):
        self._record(query, args)
        return self._fetchrow_result

    async def execute(self, query, *args):
        self._record(query, args)
        return "OK"

    def transaction(self):
        return _FakeTxn()


async def test_release_reservation_hold_releases_the_full_reserved_amount(monkeypatch, fake_release_balance):
    conn = FakeHoldReleaseConn(fetchrow_result={"reserved_kwh": Decimal("20.000"), "user_id": USER_ID})

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    result = await repo.release_reservation_hold(OPERATOR_A, RESERVATION_ID, "cancelled")

    assert result is True
    assert len(fake_release_balance) == 1
    call = fake_release_balance[0]
    assert call["amount_kwh"] == Decimal("20.000")
    assert call["user_id"] == USER_ID
    assert call["t_type"] == "release"
    assert call["conn"] is conn, "release має йти в ТІЙ САМІЙ транзакції, що UPDATE статусу"


async def test_release_reservation_hold_is_a_noop_when_already_finalized(monkeypatch, fake_release_balance):
    """
    UPDATE ... WHERE status IN ('pending','active') — якщо StopTransaction
    уже фіналізував резервацію паралельно, RETURNING нічого не дає, і
    release_reservation_hold НІЧОГО не звільняє вдруге.
    """
    conn = FakeHoldReleaseConn(fetchrow_result=None)

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    result = await repo.release_reservation_hold(OPERATOR_A, RESERVATION_ID, "expired")

    assert result is False
    assert fake_release_balance == []
