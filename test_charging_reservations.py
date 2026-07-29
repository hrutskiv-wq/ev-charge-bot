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


# ---------------------------------------------------------------------------
# Fail-closed: kWh-функції не чіпають uah-рядки (Промпт 3c-ii, правка 1 рев'ю)
# ---------------------------------------------------------------------------

async def test_release_reservation_hold_query_is_fail_closed_to_kwh(monkeypatch, fake_release_balance):
    """
    Без `payment_method = 'kwh'` у WHERE ця функція дійшла б до
    update_user_balance(user_id=NULL) для uah-рядка й впала б
    NotNullViolation — перевіряємо сам факт присутності фільтра в SQL.
    """
    conn = FakeHoldReleaseConn(fetchrow_result=None)

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    await repo.release_reservation_hold(OPERATOR_A, RESERVATION_ID, "expired")

    query, _args = conn.calls[0]
    assert "payment_method = 'kwh'" in query


async def test_list_stale_pending_reservations_query_is_fail_closed_to_kwh(monkeypatch):
    conn = FakeReservationInsertConn()  # fetch не використовується тут, лише перевіряємо SQL

    class _FetchConn(FakeReservationInsertConn):
        async def fetch(self, query, *args):
            self._record(query, args)
            return []

    fetch_conn = _FetchConn()

    async def _get_db_pool():
        return FakePool(fetch_conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    await repo.list_stale_pending_reservations(SINCE)

    query, _args = fetch_conn.calls[0]
    assert "payment_method = 'kwh'" in query


async def test_list_stale_active_reservations_query_is_fail_closed_to_kwh(monkeypatch):
    class _FetchConn(FakeReservationInsertConn):
        async def fetch(self, query, *args):
            self._record(query, args)
            return []

    fetch_conn = _FetchConn()

    async def _get_db_pool():
        return FakePool(fetch_conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    await repo.list_stale_active_reservations(SINCE)

    query, _args = fetch_conn.calls[0]
    assert "payment_method = 'kwh'" in query


# ---------------------------------------------------------------------------
# Модель B (Промпт 3c-ii) — гривневий hold через Monobank
# ---------------------------------------------------------------------------

INVOICE_ID = "inv-uah-1"


async def test_create_charging_reservation_uah_places_no_balance_hold(reservation_conn, monkeypatch):
    """
    ГОЛОВНА ВІДМІННІСТЬ від create_charging_reservation() (kWh): жодного
    виклику update_user_balance() — гроші заблоковані в БАНКУ, не в нашій
    БД (docs/plan-3c-ii.md, розділ 2).
    """
    balance_calls = []

    async def fake_update_user_balance(**kwargs):
        balance_calls.append(kwargs)
        return True

    monkeypatch.setattr(repo, "update_user_balance", fake_update_user_balance)

    reservation_id, id_tag, error = await repo.create_charging_reservation_uah(
        OPERATOR_A, STATION_ID, Decimal("100.00"), INVOICE_ID,
    )

    assert reservation_id == RESERVATION_ID
    assert error is None
    assert len(id_tag) == 16
    assert balance_calls == [], "Модель B не має чіпати kWh-баланс узагалі"

    query, args = reservation_conn.calls[0]
    assert "'uah'" in query
    assert "'awaiting_hold'" in query
    assert args == (OPERATOR_A, STATION_ID, Decimal("100.00"), INVOICE_ID, id_tag, None)


async def test_create_charging_reservation_uah_on_foreign_station_returns_none(monkeypatch):
    conn = FakeReservationInsertConn(reservation_id=None)

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    reservation_id, id_tag, error = await repo.create_charging_reservation_uah(
        OPERATOR_A, STATION_ID, Decimal("100.00"), INVOICE_ID,
    )

    assert (reservation_id, id_tag) == (None, None)
    assert error == "unknown_station"


class FakeSimpleConn:
    """Мінімальна заглушка для мʼютексних UPDATE/SELECT одним запитом."""

    def __init__(self, fetchrow_result=None, fetchval_result=None, execute_result="UPDATE 1"):
        self.calls = []
        self._fetchrow_result = fetchrow_result
        self._fetchval_result = fetchval_result
        self._execute_result = execute_result

    def _record(self, query, args):
        self.calls.append((" ".join(query.split()), args))

    async def fetchrow(self, query, *args):
        self._record(query, args)
        return self._fetchrow_result

    async def fetchval(self, query, *args):
        self._record(query, args)
        return self._fetchval_result

    async def execute(self, query, *args):
        self._record(query, args)
        return self._execute_result


async def test_get_reservation_by_invoice_id_is_scoped_to_operator(monkeypatch):
    conn = FakeSimpleConn(fetchrow_result={"id": RESERVATION_ID, "invoice_id": INVOICE_ID})

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    result = await repo.get_reservation_by_invoice_id(OPERATOR_A, INVOICE_ID)

    assert result == {"id": RESERVATION_ID, "invoice_id": INVOICE_ID}
    query, args = conn.calls[0]
    assert "operator_id = $1" in query and "invoice_id = $2" in query
    assert args == (OPERATOR_A, INVOICE_ID)


async def test_mark_reservation_hold_confirmed_is_an_idempotent_mutex(monkeypatch):
    conn = FakeSimpleConn(execute_result="UPDATE 1")

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    assert await repo.mark_reservation_hold_confirmed(OPERATOR_A, RESERVATION_ID) is True
    query, args = conn.calls[0]
    assert "status = 'pending'" in query
    assert "status = 'awaiting_hold'" in query
    assert args == (OPERATOR_A, RESERVATION_ID)

    conn2 = FakeSimpleConn(execute_result="UPDATE 0")

    async def _get_db_pool_2():
        return FakePool(conn2)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool_2)
    assert await repo.mark_reservation_hold_confirmed(OPERATOR_A, RESERVATION_ID) is False


async def test_claim_reservation_for_settlement_returns_dict_on_success(monkeypatch):
    conn = FakeSimpleConn(fetchrow_result={
        "reserved_uah": Decimal("100.00"), "invoice_id": INVOICE_ID, "operator_session_id": 555,
    })

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    result = await repo.claim_reservation_for_settlement(OPERATOR_A, RESERVATION_ID)

    assert result == {"reserved_uah": Decimal("100.00"), "invoice_id": INVOICE_ID, "operator_session_id": 555}
    query, args = conn.calls[0]
    assert "status = 'settling'" in query
    assert "payment_method = 'uah'" in query
    assert "status = $3" in query
    assert args == (OPERATOR_A, RESERVATION_ID, "active")


async def test_claim_reservation_for_settlement_accepts_expected_status_parameter(monkeypatch):
    """
    Другий блокер рев'ю: reconcile_charging_reservations.py, крок
    'pending' (_reconcile_stale_pending_uah), теж мусить взяти цю саму
    заявку — але з джерельним статусом 'pending', не 'active'.
    """
    conn = FakeSimpleConn(fetchrow_result={
        "reserved_uah": Decimal("100.00"), "invoice_id": INVOICE_ID, "operator_session_id": None,
    })

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    result = await repo.claim_reservation_for_settlement(
        OPERATOR_A, RESERVATION_ID, expected_status="pending",
    )

    assert result is not None
    query, args = conn.calls[0]
    assert "status = $3" in query
    assert args == (OPERATOR_A, RESERVATION_ID, "pending")


async def test_claim_reservation_for_settlement_returns_none_when_race_lost(monkeypatch):
    """
    Це НЕ доказ головного захисту (docs/plan-3c-ii.md, розділ 2, п.1) — мок
    просто повертає None, і функція чесно передає його далі; сам факт, що
    WHERE status='active' у реальному Postgres дійсно відсіює ДРУГОГО
    паралельного claimant'а, тут НЕ перевіряється (fetchrow_result — не
    справжній планувальник запитів). Реальна конкурентна гарантія доведена
    окремо, на живому PG: test_charging_reservations_live.py::
    test_concurrent_claim_is_won_by_exactly_one_caller (рев'ю, знахідка:
    цей тест раніше помилково називався "головним захистом").
    """
    conn = FakeSimpleConn(fetchrow_result=None)

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    assert await repo.claim_reservation_for_settlement(OPERATOR_A, RESERVATION_ID) is None


async def test_record_uah_settlement_writes_amount_and_is_idempotent(monkeypatch):
    conn = FakeSimpleConn(execute_result="UPDATE 1")

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    assert await repo.record_uah_settlement(OPERATOR_A, RESERVATION_ID, "finalized", Decimal("55.00")) is True
    query, args = conn.calls[0]
    assert "status = $3" in query
    assert "final_amount_uah = $4" in query
    assert "status <> $3" in query
    assert args == (OPERATOR_A, RESERVATION_ID, "finalized", Decimal("55.00"))

    conn2 = FakeSimpleConn(execute_result="UPDATE 0")

    async def _get_db_pool_2():
        return FakePool(conn2)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool_2)
    assert await repo.record_uah_settlement(OPERATOR_A, RESERVATION_ID, "finalized", Decimal("55.00")) is False


async def test_list_stale_awaiting_hold_reservations_query_shape(monkeypatch):
    class _FetchConn(FakeSimpleConn):
        async def fetch(self, query, *args):
            self._record(query, args)
            return []

    conn = _FetchConn()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    await repo.list_stale_awaiting_hold_reservations(SINCE)

    query, args = conn.calls[0]
    assert "status = 'awaiting_hold'" in query
    assert args == (SINCE,)


async def test_list_stale_pending_uah_reservations_query_shape(monkeypatch):
    class _FetchConn(FakeSimpleConn):
        async def fetch(self, query, *args):
            self._record(query, args)
            return []

    conn = _FetchConn()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    await repo.list_stale_pending_uah_reservations(SINCE)

    query, args = conn.calls[0]
    assert "payment_method = 'uah'" in query
    assert "status = 'pending'" in query
    assert args == (SINCE,)


async def test_list_stale_settling_reservations_query_shape(monkeypatch):
    class _FetchConn(FakeSimpleConn):
        async def fetch(self, query, *args):
            self._record(query, args)
            return []

    conn = _FetchConn()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    await repo.list_stale_settling_reservations(SINCE)

    query, args = conn.calls[0]
    assert "status = 'settling'" in query
    assert args == (SINCE,)
