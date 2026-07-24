"""
Тести ізоляції тенантів для White-Label білінгу
(app/database/operators_repo.py, міграція 0010).

Головне правило, яке тут перевіряється: оператор А ніколи не бачить і не
змінює дані оператора Б. Технічно це означає, що КОЖЕН запит до таблиць
operator_* містить у WHERE `operator_id` і отримує його параметром — забути
фільтр неможливо непомітно, бо тест пройдеться по всіх тенант-скоупнутих
функціях модуля і перевірить згенерований SQL.

Тести не піднімають реальну Postgres — підміняють пул фейковим об'єктом,
що записує, які SQL-запити й з якими параметрами були виконані (той самий
підхід, що в test_balance.py).

Запуск: pytest test_operator_isolation.py -v
"""
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from app.database import operators_repo as repo

OPERATOR_A = 1
OPERATOR_B = 2
SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Заглушки asyncpg
# ---------------------------------------------------------------------------

class FakeConnection:
    """Мінімальна заглушка asyncpg.Connection: запам'ятовує всі виклики."""

    def __init__(self, fetchrow_result=None, fetch_result=None,
                 fetchval_result=None, execute_result="UPDATE 1"):
        self.calls = []
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result if fetch_result is not None else []
        self._fetchval_result = fetchval_result
        self._execute_result = execute_result

    def _record(self, query, args):
        self.calls.append((" ".join(query.split()), args))

    async def execute(self, query, *args):
        self._record(query, args)
        return self._execute_result

    async def fetch(self, query, *args):
        self._record(query, args)
        return self._fetch_result

    async def fetchrow(self, query, *args):
        self._record(query, args)
        return self._fetchrow_result

    async def fetchval(self, query, *args):
        self._record(query, args)
        return self._fetchval_result

    def transaction(self):
        return _FakeTransaction()


class _FakeTransaction:
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


@pytest.fixture
def fake_conn(monkeypatch):
    """Підміняє пул у operators_repo фейковим з'єднанням."""
    conn = FakeConnection()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)
    return conn


def _single_call(conn):
    """Єдиний (або перший) виконаний запит: (нормалізований SQL, args)."""
    assert conn.calls, "Функція не виконала жодного запиту"
    return conn.calls[0]


# ---------------------------------------------------------------------------
# 1. Структурна перевірка: кожен тенант-скоупнутий запит фільтрує по operator_id
# ---------------------------------------------------------------------------

# (назва, як викликати з конкретним operator_id, чим саме запит звужений)
#
# Три види звуження — і кожен перевіряється своїм твердженням, а не спільним
# нестрогим "у тексті десь є operator_id":
#   "operator_id" — дочірні таблиці: WHERE operator_id = $1
#   "own_id"      — сама таблиця operators: там тенант-ключ це її ж PK, WHERE id = $1
#   "insert"      — вставка: operator_id іде першою колонкою зі значенням $1
TENANT_SCOPED_CALLS = [
    ("get_operator", lambda op_id: repo.get_operator(op_id), "own_id"),
    ("set_operator_status", lambda op_id: repo.set_operator_status(op_id, "active"), "own_id"),
    ("set_operator_monobank_token", lambda op_id: repo.set_operator_monobank_token(op_id, "enc"), "own_id"),
    ("get_operator_monobank_token_encrypted", lambda op_id: repo.get_operator_monobank_token_encrypted(op_id), "own_id"),
    ("list_stations", lambda op_id: repo.list_stations(op_id), "operator_id"),
    ("get_station", lambda op_id: repo.get_station(op_id, 10), "operator_id"),
    ("update_station_tariff", lambda op_id: repo.update_station_tariff(op_id, 10, 12.5), "operator_id"),
    ("set_station_status", lambda op_id: repo.set_station_status(op_id, 10, "offline"), "operator_id"),
    ("get_station_ocpp_auth_key_encrypted", lambda op_id: repo.get_station_ocpp_auth_key_encrypted(op_id, 10), "operator_id"),
    ("set_station_ocpp_auth_key", lambda op_id: repo.set_station_ocpp_auth_key(op_id, 10, "enc"), "operator_id"),
    ("update_station_ocpp_state", lambda op_id: repo.update_station_ocpp_state(op_id, 10, "Available"), "operator_id"),
    ("create_session", lambda op_id: repo.create_session(op_id, 10), "operator_id"),
    ("get_session", lambda op_id: repo.get_session(op_id, 77), "operator_id"),
    ("list_sessions", lambda op_id: repo.list_sessions(op_id), "operator_id"),
    ("list_sessions_by_station", lambda op_id: repo.list_sessions(op_id, station_id=10), "operator_id"),
    ("set_session_status", lambda op_id: repo.set_session_status(op_id, 77, "paid"), "operator_id"),
    ("complete_session", lambda op_id: repo.complete_session(op_id, 77, 12.0), "operator_id"),
    ("start_ocpp_transaction", lambda op_id: repo.start_ocpp_transaction(op_id, 10, 1000, SINCE), "operator_id"),
    ("complete_ocpp_transaction", lambda op_id: repo.complete_ocpp_transaction(op_id, 555, Decimal("10.500"), 20000, SINCE), "operator_id"),
    ("get_session_by_ocpp_transaction_id", lambda op_id: repo.get_session_by_ocpp_transaction_id(op_id, 555), "operator_id"),
    ("create_charging_reservation", lambda op_id: repo.create_charging_reservation(op_id, 10, 555, Decimal("20.000")), "operator_id"),
    ("get_reservation_by_session_id", lambda op_id: repo.get_reservation_by_session_id(op_id, 999), "operator_id"),
    ("activate_reservation", lambda op_id: repo.activate_reservation(op_id, 5, 999), "operator_id"),
    ("set_reservation_status", lambda op_id: repo.set_reservation_status(op_id, 5, "finalized"), "operator_id"),
    ("release_reservation_hold", lambda op_id: repo.release_reservation_hold(op_id, 5, "cancelled"), "operator_id"),
    ("create_operator_payment", lambda op_id: repo.create_operator_payment(op_id, "inv-1", 100), "insert"),
    ("get_operator_payment_by_invoice", lambda op_id: repo.get_operator_payment_by_invoice(op_id, "inv-1"), "operator_id"),
    ("get_operator_payment", lambda op_id: repo.get_operator_payment(op_id, 5), "operator_id"),
    ("set_operator_payment_status", lambda op_id: repo.set_operator_payment_status(op_id, 5, "success"), "operator_id"),
    ("attach_payment_to_session", lambda op_id: repo.attach_payment_to_session(op_id, 77, 5), "operator_id"),
    ("get_session_by_payment", lambda op_id: repo.get_session_by_payment(op_id, 5), "operator_id"),
    ("create_wallet_topup", lambda op_id: repo.create_wallet_topup(op_id, 555, "inv-w1", "pack_50", 50.0, Decimal("750.00")), "insert"),
    ("get_wallet_topup_by_invoice", lambda op_id: repo.get_wallet_topup_by_invoice(op_id, "inv-w1"), "operator_id"),
    ("set_wallet_topup_status", lambda op_id: repo.set_wallet_topup_status(op_id, 5, "success"), "operator_id"),
    ("add_ledger_entry", lambda op_id: repo.add_ledger_entry(op_id, "adjustment", 1.0), "insert"),
    ("get_operator_balance", lambda op_id: repo.get_operator_balance(op_id), "operator_id"),
    ("list_ledger", lambda op_id: repo.list_ledger(op_id), "operator_id"),
    ("list_ledger_since", lambda op_id: repo.list_ledger_since(op_id, SINCE), "operator_id"),
    ("get_ledger_summary", lambda op_id: repo.get_ledger_summary(op_id, SINCE), "operator_id"),
    ("list_pending_payments_older_than", lambda op_id: repo.list_pending_payments_older_than(op_id, SINCE), "operator_id"),
    ("list_success_payments_without_income", lambda op_id: repo.list_success_payments_without_income(op_id), "operator_id"),
    ("list_success_payments_without_session", lambda op_id: repo.list_success_payments_without_session(op_id), "operator_id"),
    ("list_stale_pending_sessions_without_payment", lambda op_id: repo.list_stale_pending_sessions_without_payment(op_id, SINCE), "operator_id"),
]

_IDS = [c[0] for c in TENANT_SCOPED_CALLS]


@pytest.mark.parametrize("name,call,scope", TENANT_SCOPED_CALLS, ids=_IDS)
async def test_every_tenant_scoped_query_is_narrowed_to_one_tenant(name, call, scope, fake_conn):
    """
    Ключовий тест ізоляції. Якщо хтось додасть функцію або перепише запит
    без звуження до одного тенанта — тест впаде тут, а не в проді на чужих даних.
    """
    await call(OPERATOR_A)
    query, args = _single_call(fake_conn)

    # operator_id завжди перший параметр — інакше легко переплутати місцями
    # при додаванні аргументів і тихо зламати фільтр.
    assert args[0] == OPERATOR_A, f"{name}: operator_id має бути першим параметром, отримано {args}"

    if scope == "operator_id":
        assert re.search(r"operator_id = \$1", query), (
            f"{name}: запит не звужений до тенанта (`operator_id = $1` відсутній):\n{query}"
        )
    elif scope == "own_id":
        # таблиця operators: рядок тенанта і є рядком, що шукаємо
        assert "FROM operators" in query or "UPDATE operators" in query, query
        assert re.search(r"WHERE id = \$1", query), (
            f"{name}: запит до operators не звужений по PK (`WHERE id = $1`):\n{query}"
        )
    else:  # insert
        assert re.search(r"INSERT INTO \w+ \(operator_id,", query), (
            f"{name}: operator_id має бути першою колонкою вставки:\n{query}"
        )
        assert "VALUES ($1," in query, (
            f"{name}: у operator_id має підставлятись саме $1:\n{query}"
        )


@pytest.mark.parametrize("name,call,scope", TENANT_SCOPED_CALLS, ids=_IDS)
async def test_operator_id_is_passed_through_verbatim(name, call, scope, fake_conn):
    """Оператор Б отримує в запиті саме свій id — жодного «дефолтного» тенанта."""
    await call(OPERATOR_B)
    _query, args = _single_call(fake_conn)
    assert args[0] == OPERATOR_B
    assert OPERATOR_A not in args[:1]


# ---------------------------------------------------------------------------
# 2. Поведінкова ізоляція: чужий ресурс = порожній результат, а не чужі дані
# ---------------------------------------------------------------------------

async def test_get_station_of_another_operator_returns_none(monkeypatch):
    """
    Станція оператора Б, запитана від імені А, не повертається. Фейк
    імітує саме поведінку Postgres: WHERE не зійшовся → жодного рядка.
    """
    station_of_b = {"id": 10, "operator_id": OPERATOR_B, "name": "Готель Б"}

    class TenantAwareConnection(FakeConnection):
        async def fetchrow(self, query, *args):
            self._record(query, args)
            # рядок повертається, лише якщо запитаний operator_id збігається
            if args[0] == station_of_b["operator_id"] and args[1] == station_of_b["id"]:
                return station_of_b
            return None

    conn = TenantAwareConnection()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    assert await repo.get_station(OPERATOR_B, 10) == station_of_b
    assert await repo.get_station(OPERATOR_A, 10) is None


async def test_update_station_tariff_of_another_operator_changes_nothing(monkeypatch):
    """Спроба А змінити тариф станції Б не оновлює жодного рядка → False."""
    class TenantAwareConnection(FakeConnection):
        async def execute(self, query, *args):
            self._record(query, args)
            return "UPDATE 1" if args[0] == OPERATOR_B else "UPDATE 0"

    conn = TenantAwareConnection()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    assert await repo.update_station_tariff(OPERATOR_B, 10, 15.0) is True
    assert await repo.update_station_tariff(OPERATOR_A, 10, 999.0) is False


async def test_create_session_derives_operator_id_from_the_station_itself(fake_conn):
    """
    Сесія не довіряє operator_id з аргументу: він підставляється в підзапит
    по станції (`INSERT ... SELECT ... WHERE s.operator_id = $1`), тому для
    чужої станції SELECT порожній і сесія не створюється. Крос-тенантний
    рядок неможливо записати навіть навмисно.
    """
    await repo.create_session(OPERATOR_A, 10)
    query, args = _single_call(fake_conn)

    assert "INSERT INTO operator_sessions" in query
    assert "FROM operator_stations s" in query
    assert "s.operator_id = $1" in query and "s.id = $2" in query
    # operator_id у вставку йде з таблиці станцій (s.operator_id), а не з аргументу
    assert "SELECT s.operator_id, s.id" in query
    assert args[0] == OPERATOR_A and args[1] == 10


async def test_create_session_on_foreign_station_returns_none(monkeypatch):
    """Станція чужа → INSERT ... SELECT не вставив нічого → None."""
    class TenantAwareConnection(FakeConnection):
        async def fetchval(self, query, *args):
            self._record(query, args)
            return 555 if args[0] == OPERATOR_B else None

    conn = TenantAwareConnection()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    assert await repo.create_session(OPERATOR_B, 10) == 555
    assert await repo.create_session(OPERATOR_A, 10) is None


# ---------------------------------------------------------------------------
# 3. Секрет оператора не тече у звичайні вибірки
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda: repo.get_operator(OPERATOR_A),
    lambda: repo.get_operator_by_telegram_id(777),
    lambda: repo.list_stations(OPERATOR_A),
])
async def test_regular_selects_never_return_the_acquiring_token(call, fake_conn):
    """
    monobank_token_encrypted має діставатись лише через окрему свідому
    функцію. Якщо він потрапить у загальний SELECT кабінету — рано чи пізно
    опиниться в лозі або в дампі для оператора.
    """
    await call()
    query, _args = _single_call(fake_conn)
    assert "monobank_token_encrypted" not in query
    assert "SELECT *" not in query


async def test_dedicated_token_getter_is_scoped_to_one_operator(fake_conn):
    await repo.get_operator_monobank_token_encrypted(OPERATOR_A)
    query, args = _single_call(fake_conn)
    assert "monobank_token_encrypted" in query
    assert "operator_id = $1" in query or "WHERE id = $1" in query
    assert args == (OPERATOR_A,)


@pytest.mark.parametrize("call", [
    lambda: repo.get_station(OPERATOR_A, 10),
    lambda: repo.list_stations(OPERATOR_A),
    lambda: repo.get_station_by_qr_slug("abc123"),
    lambda: repo.get_station_by_ocpp_charge_point_id("CP-001"),
    lambda: repo.list_public_stations_near(49.84, 24.03, radius_km=25),
])
async def test_regular_station_selects_never_return_the_ocpp_auth_key(call, fake_conn):
    """
    Той самий принцип, що вище для monobank_token_encrypted, тепер для
    ocpp_auth_key_encrypted (Промпт 3a): _STATION_FIELDS свідомо не містить
    цю колонку. list_public_stations_near і get_station_by_ocpp_charge_
    point_id особливо критичні — обидва публічні винятки, водій/станція тут
    не автентифіковані взагалі.
    """
    await call()
    query, _args = _single_call(fake_conn)
    assert "ocpp_auth_key_encrypted" not in query
    assert "SELECT *" not in query


async def test_dedicated_ocpp_auth_key_getter_is_scoped_to_one_operator(fake_conn):
    await repo.get_station_ocpp_auth_key_encrypted(OPERATOR_A, 10)
    query, args = _single_call(fake_conn)
    assert "ocpp_auth_key_encrypted" in query
    assert "operator_id = $1" in query
    assert args == (OPERATOR_A, 10)


# ---------------------------------------------------------------------------
# 4. Публічні винятки задокументовані й не приймають operator_id
# ---------------------------------------------------------------------------

async def test_public_lookups_are_keyed_by_their_own_secret(fake_conn):
    """
    get_station_by_qr_slug і get_station_by_ocpp_charge_point_id — свідомі
    винятки з правила operator_id (водій і станція не автентифіковані).
    Обидва мусять повертати operator_id, щоб подальші виклики знову були
    тенант-скоупнуті, і шукати РІВНО за своїм секретом.
    """
    await repo.get_station_by_qr_slug("abc123")
    query, args = _single_call(fake_conn)
    assert "WHERE qr_slug = $1" in query
    assert "operator_id" in query  # присутній у списку полів, що повертаються
    assert args == ("abc123",)

    fake_conn.calls.clear()
    await repo.get_station_by_ocpp_charge_point_id("CP-001")
    query, args = _single_call(fake_conn)
    assert "WHERE ocpp_charge_point_id = $1" in query
    assert "operator_id" in query
    assert args == ("CP-001",)


async def test_list_public_stations_near_is_a_public_exception_filtered_by_geography(fake_conn):
    """
    Третій свідомий виняток з правила ізоляції (Промпт 4c): публічний пошук
    станцій для водія. Не operator_id, а активність станції/оператора й
    наявність координат — фільтри в SQL; кожен рядок так само несе
    operator_id, щоб подальший перехід на /s/{qr_slug} лишався тенант-
    скоупнутим.
    """
    await repo.list_public_stations_near(49.84, 24.03, radius_km=25)
    query, args = _single_call(fake_conn)

    assert "operator_id" in query
    assert "JOIN operators" in query
    assert "s.status = 'active'" in query
    assert "o.status = 'active'" in query
    assert "s.lat IS NOT NULL" in query and "s.lng IS NOT NULL" in query
    assert args == (), "Функція не приймає operator_id — фільтр географічний, не тенантний"


async def test_list_public_stations_near_filters_by_radius_and_sorts_by_distance(monkeypatch):
    rows = [
        {"id": 1, "operator_id": 10, "name": "Близька", "address": None,
         "lat": Decimal("49.8400"), "lng": Decimal("24.0300"), "connector_type": "Type 2",
         "power_kw": Decimal("22"), "mode": "manual", "ocpp_charge_point_id": None,
         "tariff_uah_kwh": Decimal("12.50"), "tariff_uah_start": None,
         "qr_slug": "s1", "status": "active", "created_at": None},
        {"id": 2, "operator_id": 11, "name": "Київ (далеко)", "address": None,
         "lat": Decimal("50.4501"), "lng": Decimal("30.5234"), "connector_type": "CCS",
         "power_kw": Decimal("60"), "mode": "manual", "ocpp_charge_point_id": None,
         "tariff_uah_kwh": Decimal("15.00"), "tariff_uah_start": None,
         "qr_slug": "s2", "status": "active", "created_at": None},
        {"id": 3, "operator_id": 10, "name": "Середня", "address": None,
         "lat": Decimal("49.9035"), "lng": Decimal("24.1097"), "connector_type": "Schuko",
         "power_kw": None, "mode": "manual", "ocpp_charge_point_id": None,
         "tariff_uah_kwh": Decimal("8.00"), "tariff_uah_start": None,
         "qr_slug": "s3", "status": "active", "created_at": None},
    ]
    conn = FakeConnection(fetch_result=rows)

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    # Пошук від Львова, радіус 20 км — рядок з Києва (id=2) не влазить.
    result = await repo.list_public_stations_near(49.8397, 24.0297, radius_km=20)

    assert [r["id"] for r in result] == [1, 3]
    assert result[0]["distance_km"] < result[1]["distance_km"]
    assert all("operator_id" in r for r in result)
    assert all("distance_km" in r for r in result)


async def test_list_public_stations_near_returns_empty_when_nothing_in_radius(monkeypatch):
    rows = [
        {"id": 1, "operator_id": 10, "name": "Дуже далеко", "address": None,
         "lat": Decimal("50.4501"), "lng": Decimal("30.5234"), "connector_type": None,
         "power_kw": None, "mode": "manual", "ocpp_charge_point_id": None,
         "tariff_uah_kwh": Decimal("10.00"), "tariff_uah_start": None,
         "qr_slug": "s1", "status": "active", "created_at": None},
    ]
    conn = FakeConnection(fetch_result=rows)

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    result = await repo.list_public_stations_near(49.8397, 24.0297, radius_km=5)
    assert result == []


# ---------------------------------------------------------------------------
# 5. Журнал розрахунків
# ---------------------------------------------------------------------------

async def test_record_session_income_writes_income_and_commission_in_one_transaction(monkeypatch):
    """
    Дохід і комісія пишуться двома рядками через ОДНЕ з'єднання (тобто в
    одній транзакції) — дохід не може існувати без комісії. Комісія завжди
    від'ємна: журнал знаковий, як kw_transactions.
    """
    # fetchval повертає id (не None) — тобто вставка пройшла без конфлікту
    # і гілка «сесію вже проводили» не вмикається.
    fake_conn = FakeConnection(fetchval_result=101)

    async def _get_db_pool():
        return FakePool(fake_conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    await repo.record_session_income(OPERATOR_A, session_id=77, amount_uah=300.0,
                                     commission_pct=4)

    assert len(fake_conn.calls) == 2, "Обидва записи мають іти через один conn"

    (_q1, income_args), (_q2, commission_args) = fake_conn.calls
    # args: (operator_id, session_id, type, amount_uah, description)
    assert income_args[0] == OPERATOR_A and commission_args[0] == OPERATOR_A
    assert income_args[2] == "session_income"
    assert income_args[3] == 300.0
    assert commission_args[2] == "platform_commission"
    assert commission_args[3] == -12.0  # 4% від 300 грн, від'ємним числом


class FakeLedgerConnection(FakeConnection):
    """
    З'єднання, що імітує реальну поведінку часткового унікального індексу
    uq_ledger_session_income: повторна вставка (session_id, type) для
    доходу/комісії відхиляється, ON CONFLICT DO NOTHING ... RETURNING id
    не повертає нічого (None).
    """

    def __init__(self):
        super().__init__()
        self.rows = []
        self._next_id = 100

    async def fetchval(self, query, *args):
        self._record(query, args)
        assert "INSERT INTO operator_payout_ledger" in query
        assert "ON CONFLICT DO NOTHING" in query, "Без ON CONFLICT вставка впала б з помилкою"
        operator_id, session_id, entry_type, amount_uah, description = args

        constrained = session_id is not None and entry_type in (
            "session_income", "platform_commission",
        )
        if constrained and any(
            r["session_id"] == session_id and r["type"] == entry_type for r in self.rows
        ):
            return None  # конфлікт унікального індексу

        self._next_id += 1
        self.rows.append({
            "id": self._next_id, "operator_id": operator_id, "session_id": session_id,
            "type": entry_type, "amount_uah": amount_uah, "description": description,
        })
        return self._next_id

    async def fetch(self, query, *args):
        self._record(query, args)
        operator_id, session_id = args
        return [
            r for r in self.rows
            if r["operator_id"] == operator_id and r["session_id"] == session_id
            and r["type"] in ("session_income", "platform_commission")
        ]


async def test_record_session_income_is_idempotent_on_repeated_call(monkeypatch, caplog):
    """
    Повторний виклик з тим самим session_id (повторний webhook Monobank,
    ретрай, подвійний клік) НЕ нараховує дохід удруге: у журналі лишається
    рівно 2 рядки, а повернені id — ті самі, що з першого разу.

    Це критично саме для цього журналу: він незмінний, тож зайве
    нарахування довелось би потім гасити ручним рядком 'adjustment'.
    """
    conn = FakeLedgerConnection()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    first = await repo.record_session_income(OPERATOR_A, session_id=77,
                                             amount_uah=300.0, commission_pct=4)
    with caplog.at_level("WARNING", logger="app.database.operators_repo"):
        second = await repo.record_session_income(OPERATOR_A, session_id=77,
                                                  amount_uah=300.0, commission_pct=4)

    assert len(conn.rows) == 2, f"У журналі має бути рівно 2 рядки, а не {len(conn.rows)}"
    assert first == second, f"Повторний виклик повернув інші id: {first} != {second}"
    assert all(i is not None for i in second)

    # сума журналу не зросла від повторного проведення.
    # Рахуємо через Decimal: дохід приходить як float від викликача, комісія
    # вже Decimal — складати їх напряму не можна (TypeError).
    total = sum(Decimal(str(r["amount_uah"])) for r in conn.rows)
    assert total == Decimal("288.00")  # 300.00 доходу мінус 12.00 комісії
    assert "duplicate income ignored" in caplog.text


@pytest.mark.parametrize("amount_uah,commission_pct,expected", [
    # межовий випадок: рівно половина копійки, має лишитись як є
    (250, 4.5, "11.25"),
    # типовий «некруглий» чек: 13.3332 -> 13.33
    (333.33, 4, "13.33"),
    # РЕГРЕСІЯ: саме тут старий round() давав 5.62 замість 5.63 —
    # банківське округлення до парного зрізало копійку не на нашу користь
    (112.5, 5, "5.63"),
    (300, 4, "12.00"),
])
async def test_commission_is_rounded_half_up_in_decimal(amount_uah, commission_pct,
                                                        expected, monkeypatch):
    """
    Комісія рахується в Decimal з ROUND_HALF_UP, а не round() по float.
    Перевіряємо саме записане в журнал значення, а не проміжний результат.
    """
    conn = FakeLedgerConnection()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    await repo.record_session_income(OPERATOR_A, session_id=77,
                                     amount_uah=amount_uah, commission_pct=commission_pct)

    commission_row = next(r for r in conn.rows if r["type"] == "platform_commission")
    written = commission_row["amount_uah"]

    assert isinstance(written, Decimal), (
        f"У журнал має йти Decimal, а не {type(written).__name__} — "
        "інакше двійкова похибка float потрапляє в гроші"
    )
    # порівнюємо рядком: Decimal('11.25') == Decimal('11.250'), але нам
    # важливо, що значення заквантоване рівно до копійок
    assert str(-written) == expected
    assert -written == Decimal(expected)


async def test_commission_sign_and_scale_are_stable(monkeypatch):
    """Комісія завжди від'ємна і завжди рівно з двома знаками після коми."""
    conn = FakeLedgerConnection()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    await repo.record_session_income(OPERATOR_A, session_id=77, amount_uah=100,
                                     commission_pct=3)
    commission_row = next(r for r in conn.rows if r["type"] == "platform_commission")
    assert commission_row["amount_uah"] < 0
    assert commission_row["amount_uah"].as_tuple().exponent == -2


async def test_different_sessions_are_recorded_independently(monkeypatch):
    """Обмеження стосується однієї сесії, а не оператора: інша сесія проводиться нормально."""
    conn = FakeLedgerConnection()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    first = await repo.record_session_income(OPERATOR_A, session_id=77,
                                             amount_uah=300.0, commission_pct=4)
    second = await repo.record_session_income(OPERATOR_A, session_id=78,
                                              amount_uah=150.0, commission_pct=4)

    assert len(conn.rows) == 4
    assert set(first).isdisjoint(set(second))


async def test_entries_without_session_id_are_not_constrained(monkeypatch):
    """
    Виплати й підписки не мають session_id, тому частковий індекс їх не
    покриває — оператору можна виплатити двічі, це нормальна операція.
    """
    conn = FakeLedgerConnection()

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    first = await repo.add_ledger_entry(OPERATOR_A, "payout", -500.0)
    second = await repo.add_ledger_entry(OPERATOR_A, "payout", -500.0)

    assert first is not None and second is not None and first != second
    assert len(conn.rows) == 2


async def test_payment_status_update_touches_updated_at(fake_conn):
    """
    updated_at має рухатись при кожній зміні статусу — без цього неможливо
    відповісти «коли саме цей платіж став success», а це перше питання
    будь-якого розбору розбіжності зі звіркою.
    """
    await repo.set_operator_payment_status(OPERATOR_A, 5, "success")
    query, _args = _single_call(fake_conn)

    assert "UPDATE operator_payments" in query
    assert "updated_at = CURRENT_TIMESTAMP" in query


async def test_payment_status_update_is_idempotent_by_status_guard(fake_conn):
    """
    `status <> $3` — це мʼютекс, на якому тримається захист від подвійного
    нарахування у webhook: повторний виклик з тим самим статусом не змінює
    рядок, викликач отримує False і не проводить дохід удруге.
    """
    await repo.set_operator_payment_status(OPERATOR_A, 5, "success")
    query, _args = _single_call(fake_conn)
    assert "status <> $3" in query


async def test_ledger_entry_accepts_caller_transaction():
    """
    add_ledger_entry(conn=...) пише в транзакції викликача, не відкриваючи
    власного з'єднання — та сама механіка, що в update_user_balance().
    Пул тут навмисно не підмінений: якби функція полізла по нього, тест би впав.
    """
    conn = FakeConnection(fetchval_result=42)
    entry_id = await repo.add_ledger_entry(
        OPERATOR_A, "payout", -500.0, description="Виплата на рахунок", conn=conn,
    )
    assert entry_id == 42
    query, args = _single_call(conn)
    assert "INSERT INTO operator_payout_ledger" in query
    assert args[0] == OPERATOR_A


async def test_balance_is_summed_from_the_ledger_not_a_cached_column(fake_conn):
    """Балансу як колонки не існує — лише SUM журналу по одному оператору."""
    await repo.get_operator_balance(OPERATOR_A)
    query, args = _single_call(fake_conn)
    assert "SUM(amount_uah)" in query
    assert "FROM operator_payout_ledger" in query
    assert "operator_id = $1" in query
    assert args == (OPERATOR_A,)


# ---------------------------------------------------------------------------
# 6. Alembic-міграція і idempotent-бутстрап не розходяться
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent
# Схема білінгу розкладена по кількох ревізіях (0010 — тенанти й сесії,
# 0011 — платежі водіїв), а дзеркало init_operator_tables() одне. Тому
# порівнюємо ОБ'ЄДНАННЯ міграцій із дзеркалом: інакше кожна нова ревізія
# «ламала» б звірку, і її б швидко вимкнули.
_MIGRATION_FILES = [
    _ROOT / "migrations" / "versions" / "0010_white_label_tenants.py",
    _ROOT / "migrations" / "versions" / "0011_operator_payments.py",
    _ROOT / "migrations" / "versions" / "0012_wallet_topups.py",
    _ROOT / "migrations" / "versions" / "0013_ocpp_station_fields.py",
    _ROOT / "migrations" / "versions" / "0014_ocpp_transactions.py",
    _ROOT / "migrations" / "versions" / "0015_hold_release_transaction_types.py",
    _ROOT / "migrations" / "versions" / "0016_charging_reservations.py",
]
_REPO_FILE = _ROOT / "app" / "database" / "operators_repo.py"


def _migrations_source() -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in _MIGRATION_FILES)

_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\s*\);", re.DOTALL)
# Ловимо і звичайні, і UNIQUE-індекси, і порівнюємо ВЕСЬ текст визначення
# разом із WHERE — інакше часткові унікальні індекси (uq_ledger_session_income)
# пройшли б повз звірку, а саме вони й тримають ідемпотентність.
_INDEX_RE = re.compile(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS .+?;")
# Колонки, додані вже ПІСЛЯ початкового CREATE TABLE (0010), через
# ALTER TABLE ADD COLUMN IF NOT EXISTS — 0013 (OCPP, Промпт 3a) саме такий
# випадок: operator_stations уже існує, тому нові поля йдуть ALTER-ом, а не
# переписуванням CREATE TABLE 0010. _TABLE_RE вище такі рядки не бачить
# взагалі, тому для НИХ потрібна окрема звірка — інакше та сама розбіжність
# "є в бутстрапі, немає в міграції" (урок 'refund', PROJECT_CONTEXT.md)
# могла б повторитись і лишитись непоміченою.
_ALTER_COLUMN_RE = re.compile(r"ALTER TABLE \w+ ADD COLUMN IF NOT EXISTS .+?;")
_NOT_A_COLUMN = {"unique", "foreign", "primary", "check", "constraint", "references"}


def _declared_indexes(source: str):
    return set(_INDEX_RE.findall(" ".join(source.split())))


def _declared_alter_columns(source: str):
    return set(_ALTER_COLUMN_RE.findall(" ".join(source.split())))


def _declared_columns(source: str):
    """{таблиця: {колонки}} з усіх CREATE TABLE у файлі."""
    tables = {}
    for table, body in _TABLE_RE.findall(source):
        columns = set()
        for line in body.splitlines():
            first = line.strip().split(" ")[0].strip(",")
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", first):
                continue
            if first in _NOT_A_COLUMN:
                continue
            columns.add(first)
        tables[table] = columns
    return tables


def test_migration_and_idempotent_bootstrap_declare_same_columns():
    """
    Конвенція проєкту: схема живе у двох місцях (Alembic + idempotent-блок),
    і вони мають збігатися. Саме їх розходження — причина бага з 'refund'
    (PROJECT_CONTEXT.md, п.8): рядок був у бутстрапі, але не в міграції, тож
    на проді значення насправді не існувало. Цей тест ловить такий розхід
    одразу, а не через місяць на живій базі.
    """
    migration_tables = _declared_columns(_migrations_source())
    repo_tables = _declared_columns(_REPO_FILE.read_text(encoding="utf-8"))

    expected = {"operators", "operator_stations", "operator_sessions",
                "operator_payout_ledger", "operator_payments", "wallet_topups",
                "charging_reservations"}
    assert set(migration_tables) == expected
    assert set(repo_tables) == expected

    for table in expected:
        assert migration_tables[table] == repo_tables[table], (
            f"Схема таблиці {table} розійшлася між міграцією 0010 і "
            f"init_operator_tables(). Лише в міграції: "
            f"{migration_tables[table] - repo_tables[table]}; лише в бутстрапі: "
            f"{repo_tables[table] - migration_tables[table]}"
        )


def test_migration_and_idempotent_bootstrap_declare_same_indexes():
    migration_indexes = _declared_indexes(_migrations_source())
    repo_indexes = _declared_indexes(_REPO_FILE.read_text(encoding="utf-8"))
    assert migration_indexes == repo_indexes


def test_migration_and_idempotent_bootstrap_declare_same_altered_columns():
    """
    Той самий контроль, що й test_migration_and_idempotent_bootstrap_
    declare_same_columns(), але для колонок, доданих через ALTER TABLE
    ADD COLUMN IF NOT EXISTS: 0013 (три поля OCPP на operator_stations,
    Промпт 3a) + 0014 (три поля OCPP-транзакцій на operator_sessions,
    Промпт 3b), а не переписуванням CREATE TABLE.
    """
    migration_altered = _declared_alter_columns(_migrations_source())
    repo_altered = _declared_alter_columns(_REPO_FILE.read_text(encoding="utf-8"))
    assert migration_altered == repo_altered
    assert len(migration_altered) == 6, "Очікувались рівно 6 нових полів OCPP (Промпти 3a+3b)"


def test_idempotency_is_guaranteed_by_partial_unique_indexes():
    """
    Ідемпотентність нарахувань тримається на БД, а не лише на коді: навіть
    якщо викликач обійде record_session_income(), другий дохід по сесії не
    запишеться. Індекси часткові — рядки без session_id ('payout',
    'subscription_fee', 'adjustment') під обмеження не підпадають.
    """
    for source in (_migrations_source(), _REPO_FILE.read_text(encoding="utf-8")):
        indexes = _declared_indexes(source)
        income = [i for i in indexes if "uq_ledger_session_income" in i]
        payment = [i for i in indexes if "uq_sessions_payment" in i]

        assert len(income) == 1, "Немає унікального індексу на дохід/комісію сесії"
        assert "UNIQUE" in income[0]
        assert "operator_payout_ledger(session_id, type)" in income[0]
        assert "WHERE session_id IS NOT NULL" in income[0], "Індекс має бути ЧАСТКОВИМ"
        assert "'session_income', 'platform_commission'" in income[0]

        assert len(payment) == 1, "Немає унікального індексу на payment_id сесії"
        assert "UNIQUE" in payment[0]
        assert "operator_sessions(payment_id)" in payment[0]
        assert "WHERE payment_id IS NOT NULL" in payment[0]


def test_operators_has_no_redundant_unique_on_primary_key():
    """
    UNIQUE (id) на operators був надлишковим — PRIMARY KEY уже дає і
    унікальність, і індекс. Прибрано за результатом рев'ю; тест фіксує, щоб
    не повернулось копіпастом з operator_stations, де UNIQUE (id, operator_id)
    справді потрібен (мішень композитного FK).
    """
    for source in (_migrations_source(), _REPO_FILE.read_text(encoding="utf-8")):
        operators_body = re.search(
            r"CREATE TABLE IF NOT EXISTS operators \((.*?)\n\s*\);", source, re.DOTALL
        ).group(1)
        assert "UNIQUE (id)" not in operators_body
        assert "id SERIAL PRIMARY KEY" in operators_body


def test_every_tenant_table_carries_operator_id():
    """Правило «кожна таблиця з operator_id» — перевіряємо саме на схемі."""
    tables = _declared_columns(_migrations_source())
    for table in ("operator_stations", "operator_sessions", "operator_payout_ledger",
                  "operator_payments", "wallet_topups", "charging_reservations"):
        assert "operator_id" in tables[table], f"{table} без operator_id"
    # у самій таблиці операторів роль operator_id грає її ж первинний ключ
    assert "id" in tables["operators"]


def test_sessions_are_bound_to_stations_by_composite_foreign_key():
    """
    Композитний FK (station_id, operator_id) → operator_stations(id, operator_id)
    робить крос-тенантну сесію неможливою на рівні БД, а не лише в коді.
    Він працює лише за наявності UNIQUE (id, operator_id) на станціях —
    перевіряємо обидві половини разом, бо поодинці вони безглузді.
    """
    for source in (_migrations_source(), _REPO_FILE.read_text(encoding="utf-8")):
        normalized = " ".join(source.split())
        assert "UNIQUE (id, operator_id)" in normalized
        assert (
            "FOREIGN KEY (station_id, operator_id) REFERENCES operator_stations (id, operator_id)"
            in normalized
        )


# ---------------------------------------------------------------------------
# 7. OCPP-транзакції (Промпт 3b) — ідемпотентність start_ocpp_transaction
# ---------------------------------------------------------------------------

class _FakeOcppTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeOcppSessionConn:
    """
    Заглушка для start_ocpp_transaction(): перший fetchrow — перевірка "чи
    вже є 'charging'-сесія". Якщо немає — fetchval (INSERT ... RETURNING
    id) + execute (UPDATE ... SET ocpp_transaction_id), обидва в
    conn.transaction() (хотфікс: два кроки в одній транзакції замість
    одного writable-CTE запиту, що на реальному Postgres мовчки не бачив
    щойно вставлений рядок — див. докстрінг start_ocpp_transaction).
    raise_unique_violation_once симулює справжню гонку: fetchval (INSERT)
    кидає UniqueViolationError, функція має перечитати переможця гонки
    ТИМ САМИМ SELECT-ом, що й на вході.
    """

    def __init__(self, existing=None, new_session_id=None, raise_unique_violation_once=False):
        self.calls = []
        self.existing = existing
        self.new_session_id = new_session_id
        self.raise_unique_violation_once = raise_unique_violation_once
        self._raised = False
        self._select_calls = 0

    def _record(self, query, args):
        self.calls.append((" ".join(query.split()), args))

    async def fetchrow(self, query, *args):
        self._record(query, args)
        self._select_calls += 1
        if self._select_calls == 1:
            return self.existing
        # Другий fetchrow (лише після пійманого UniqueViolationError) —
        # переможець гонки.
        return {"id": self.new_session_id, "ocpp_transaction_id": self.new_session_id}

    async def fetchval(self, query, *args):
        self._record(query, args)
        if self.raise_unique_violation_once and not self._raised:
            self._raised = True
            raise asyncpg.exceptions.UniqueViolationError("duplicate key")
        return self.new_session_id

    async def execute(self, query, *args):
        self._record(query, args)
        return "UPDATE 1"

    def transaction(self):
        return _FakeOcppTxn()


async def test_start_ocpp_transaction_creates_a_new_session_when_none_active(monkeypatch):
    conn = FakeOcppSessionConn(existing=None, new_session_id=42)

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    session_id, transaction_id, is_new = await repo.start_ocpp_transaction(
        OPERATOR_A, 10, 1000, SINCE,
    )

    assert (session_id, transaction_id, is_new) == (42, 42, True)
    assert len(conn.calls) == 3, "Мало бути: перевірка наявної сесії + INSERT + UPDATE"
    assert "INSERT INTO operator_sessions" in conn.calls[1][0]
    assert "UPDATE operator_sessions" in conn.calls[2][0]
    assert conn.calls[2][1] == (42,), "UPDATE має виставити ocpp_transaction_id саме на id щойно вставленої сесії"


async def test_start_ocpp_transaction_is_idempotent_when_already_charging(monkeypatch):
    """
    Ретрай StartTransaction (станція не отримала ack і повторила запит):
    уже є 'charging'-сесія з призначеним transactionId — функція повертає
    ЇЇ, не чіпаючи БД другим запитом (INSERT взагалі не виконується).
    """
    conn = FakeOcppSessionConn(existing={"id": 7, "ocpp_transaction_id": 7})

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    session_id, transaction_id, is_new = await repo.start_ocpp_transaction(
        OPERATOR_A, 10, 1000, SINCE,
    )

    assert (session_id, transaction_id, is_new) == (7, 7, False)
    assert len(conn.calls) == 1, "Повторний старт не мав чіпати БД другим запитом (INSERT)"


async def test_start_ocpp_transaction_resolves_genuine_race_via_unique_violation(monkeypatch):
    """
    Справжня гонка (не ретрай CP, а два конкурентних виклики майже
    одночасно): перший SELECT нічого не бачить (обидва стартували до того,
    як хтось встиг закомітити), наш INSERT ловить UniqueViolationError
    від часткового унікального індексу (conn.transaction() відкочує
    незавершений INSERT) — і функція перечитує переможця гонки замість
    того, щоб впасти або створити другу сесію.
    """
    conn = FakeOcppSessionConn(
        existing=None,
        new_session_id=99,
        raise_unique_violation_once=True,
    )

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    session_id, transaction_id, is_new = await repo.start_ocpp_transaction(
        OPERATOR_A, 10, 1000, SINCE,
    )

    assert (session_id, transaction_id, is_new) == (99, 99, False)
    assert "INSERT INTO operator_sessions" in conn.calls[1][0]
    assert len(conn.calls) == 3, "Перевірка наявної сесії + невдалий INSERT + перечитування переможця гонки"


async def test_start_ocpp_transaction_on_foreign_station_returns_none(monkeypatch):
    """Станція не належить operator_id -> INSERT ... SELECT нічого не вставив (як create_session)."""
    conn = FakeOcppSessionConn(existing=None, new_session_id=None)

    async def _get_db_pool():
        return FakePool(conn)

    monkeypatch.setattr(repo, "get_db_pool", _get_db_pool)

    result = await repo.start_ocpp_transaction(OPERATOR_A, 10, 1000, SINCE)
    assert result == (None, None, False)
    assert len(conn.calls) == 2, "Перевірка наявної сесії + INSERT (UPDATE не мав виконатись)"


async def test_complete_ocpp_transaction_is_idempotent_by_status_guard(fake_conn):
    """`status <> 'completed'` — та сама мʼютекс-умова, що set_wallet_topup_status."""
    await repo.complete_ocpp_transaction(OPERATOR_A, 555, Decimal("10.500"), 20000, SINCE)
    query, args = _single_call(fake_conn)
    assert "status <> 'completed'" in query
    assert "ocpp_transaction_id = $2" in query
    assert args[0] == OPERATOR_A and args[1] == 555
