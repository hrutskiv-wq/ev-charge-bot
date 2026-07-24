"""
Інтеграційний регресійний тест на start_ocpp_transaction() ПРОТИ РЕАЛЬНОГО
Postgres — свідомий виняток із конвенції "тести без живої БД".

Чому саме тут: клас багів "writable CTE, де головний UPDATE не бачить
рядок, щойно вставлений сусідньою CTE в тій самій WITH-конструкції (знімок
береться ДО виконання всього запиту)" принципово НЕВИДИМИЙ для мокнутого
репозиторію — fake_repo (test_ocpp_transactions.py) і FakeOcppSessionConn
(test_operator_isolation.py) підміняють саму функцію/її SQL-виклики
Python-заглушками, тому реальний текст запиту під ними НІКОЛИ не
виконувався проти справжнього планувальника Postgres. Саме тому баг
(WITH new_row AS (INSERT ... RETURNING id) UPDATE ... FROM new_row —
UPDATE ловив 0 рядків, ocpp_transaction_id лишався NULL, RETURNING
повертав порожньо, хоча INSERT реально вставляв 'charging'-рядок) дожив
до живого прогону OCPP 3c-i (2026-07-24) непоміченим і закритий окремим
хотфіксом (два кроки — INSERT ... RETURNING id, потім UPDATE ... SET
ocpp_transaction_id — в одній conn.transaction()).

Потребує живого Postgres, доступного через DB_URL (той самий env var, що
й app/database/connection.py) — якщо не задано, тест пропускається (щоб
не ламати звичайний pytest-прогін/CI без БД).

Запуск локально:
    docker compose up -d postgres
    # POSTGRES_USER/PASSWORD/DB — з .env
    DB_URL=postgresql://<user>:<pass>@127.0.0.1:5432/<db> \\
        pytest test_start_ocpp_transaction_live.py -v
"""
import asyncio
import os
import secrets
from datetime import datetime, timezone

import pytest

from app.database import connection
from app.database import operators_repo as repo

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL"), reason="потрібен живий Postgres (DB_URL) — див. докстрінг файлу",
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def live_station():
    await connection.init_postgres()
    await repo.init_operator_tables()

    telegram_id = int.from_bytes(secrets.token_bytes(4), "big")
    operator_id = await repo.create_operator(name="live-test-3b-hotfix", telegram_id=telegram_id)
    station_id, _qr_slug = await repo.create_station(
        operator_id, "live-test-station", 10.0, mode="ocpp",
        ocpp_charge_point_id=f"CP-LIVE-TEST-{telegram_id}",
    )

    yield operator_id, station_id

    pool = await connection.get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM operator_sessions WHERE operator_id = $1", operator_id)
        await conn.execute("DELETE FROM operator_stations WHERE operator_id = $1", operator_id)
        await conn.execute("DELETE FROM operators WHERE id = $1", operator_id)


async def test_start_ocpp_transaction_sets_ocpp_transaction_id_on_real_postgres(live_station):
    """
    Регресія на CTE-баг (2026-07-24): щойно вставлена 'charging'-сесія МАЄ
    отримати ocpp_transaction_id == власному id в ТІЙ САМІЙ атомарній
    операції. На попередній реалізації (WITH new_row AS (INSERT ...
    RETURNING id) UPDATE ... FROM new_row) цей тест падав на самому
    першому assert — session_id повертався None, хоча рядок реально вже
    існував у БД (осиротіла 'charging'-сесія з ocpp_transaction_id=NULL).
    """
    operator_id, station_id = live_station

    session_id, transaction_id, is_new = await repo.start_ocpp_transaction(
        operator_id, station_id, meter_start_wh=1000, started_at=NOW,
    )

    assert session_id is not None, (
        "На попередній (writable-CTE) реалізації тут повертався None, хоча "
        "рядок УЖЕ реально існував у operator_sessions"
    )
    assert transaction_id == session_id
    assert is_new is True

    pool = await connection.get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, ocpp_transaction_id FROM operator_sessions WHERE id = $1", session_id,
        )
    assert row["status"] == "charging"
    assert row["ocpp_transaction_id"] == session_id, (
        "Пряма перевірка в БД — саме це поле лишалось NULL на попередній "
        "реалізації (осиротіла 'charging'-сесія без transactionId)"
    )


async def test_start_ocpp_transaction_retry_returns_the_same_transaction_id(live_station):
    """
    Ретрай StartTransaction (станція не отримала ack і шле те саме
    StartTransaction вдруге) — другий виклик має повернути ТУ САМУ сесію
    й transactionId, is_new=False, без створення другого рядка.
    """
    operator_id, station_id = live_station

    first = await repo.start_ocpp_transaction(operator_id, station_id, meter_start_wh=1000, started_at=NOW)
    second = await repo.start_ocpp_transaction(operator_id, station_id, meter_start_wh=1000, started_at=NOW)

    assert first[0] is not None
    assert second == (first[0], first[1], False)

    pool = await connection.get_db_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM operator_sessions WHERE operator_id = $1 AND station_id = $2",
            operator_id, station_id,
        )
    assert count == 1, "Ретрай не мав створити другий рядок сесії"


async def test_concurrent_start_is_blocked_by_the_partial_unique_index(live_station):
    """
    Справжня гонка (два ПАРАЛЕЛЬНІ виклики, не послідовний ретрай) — обидва
    можуть пройти повз верхній SELECT одночасно (жоден ще не закомітив),
    але лише ОДИН реально вставляє рядок; другий ловить UniqueViolationError
    від uq_operator_sessions_one_active_ocpp_per_station і повертає
    результат переможця гонки. До хотфіксу ця гілка була МЕРТВИМ кодом —
    ocpp_transaction_id завжди лишався NULL, тож умова часткового індексу
    (status='charging' AND ocpp_transaction_id IS NOT NULL) ніколи не
    спрацьовувала, і два конкурентні виклики тихо створювали ДВІ 'charging'
    сесії на одну станцію.
    """
    operator_id, station_id = live_station

    results = await asyncio.gather(
        repo.start_ocpp_transaction(operator_id, station_id, meter_start_wh=1000, started_at=NOW),
        repo.start_ocpp_transaction(operator_id, station_id, meter_start_wh=1000, started_at=NOW),
    )

    transaction_ids = {r[1] for r in results}
    assert len(transaction_ids) == 1, "Гонка мала дати ОДНУ спільну сесію, а не дві"
    assert sum(1 for r in results if r[2] is True) == 1, "Рівно один виклик мав створити нову сесію"

    pool = await connection.get_db_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM operator_sessions WHERE operator_id = $1 AND station_id = $2",
            operator_id, station_id,
        )
    assert count == 1, (
        "До хотфіксу тут було 2 — індекс не спрацьовував, бо ocpp_transaction_id "
        "завжди лишався NULL"
    )


async def test_start_ocpp_transaction_on_foreign_station_returns_none(live_station):
    operator_id, _station_id = live_station
    foreign_operator_id = operator_id + 1_000_000  # свідомо неіснуючий оператор

    result = await repo.start_ocpp_transaction(
        foreign_operator_id, _station_id, meter_start_wh=1000, started_at=NOW,
    )
    assert result == (None, None, False)
