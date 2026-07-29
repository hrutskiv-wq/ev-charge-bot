"""
Інтеграційний регресійний тест на claim_reservation_for_settlement()
ПРОТИ РЕАЛЬНОГО Postgres — свідомий виняток із конвенції "тести без живої
БД", за зразком test_start_ocpp_transaction_live.py.

Чому саме тут: claim_reservation_for_settlement() — головний захист
Моделі B (Промпт 3c-ii) від подвійного дзвінка в банк (docs/plan-3c-ii.md,
розділ 2, п.1) — атомарний мʼютекс `UPDATE ... WHERE status = 'active'
... RETURNING`. Мокнутий репозиторій (test_charging_reservations.py)
підміняє САМУ функцію get_db_pool()/fetchrow фейком, який лише повертає
заготовлений результат — він доводить, що функція коректно ОБРОБЛЯЄ
None/dict, але НЕ доводить, що WHERE status='active' дійсно відсіює
ДРУГОГО паралельного претендента на реальному планувальнику Postgres.
Рев'ю бандла 3c-ii вимагало саме цього доказу: перевірено вручну двома
паралельними сесіями на живому PG (поведінка правильна), тут те саме
зафіксовано як регресійний тест.

Потребує живого Postgres, доступного через DB_URL (той самий env var, що
й app/database/connection.py) — якщо не задано, тест пропускається.

Запуск локально:
    docker compose up -d postgres
    DB_URL=postgresql://<user>:<pass>@127.0.0.1:5432/<db> \\
        pytest test_charging_reservations_live.py -v
"""
import asyncio
import os
import secrets

import pytest

from app.database import connection
from app.database import operators_repo as repo

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL"), reason="потрібен живий Postgres (DB_URL) — див. докстрінг файлу",
)


@pytest.fixture
async def live_active_uah_reservation():
    """
    Готова 'active' uah-резервація, вставлена НАПРЯМУ SQL (не через
    create_charging_reservation_uah(), яка створює лише 'awaiting_hold' —
    реальний перехід у 'active' відбувається через вебхук+StartTransaction,
    тут потрібен лише кінцевий стан для перевірки claim-мʼютексу).
    """
    await connection.init_postgres()
    await repo.init_operator_tables()

    telegram_id = int.from_bytes(secrets.token_bytes(4), "big")
    operator_id = await repo.create_operator(name="live-test-3c-ii-claim", telegram_id=telegram_id)
    station_id, _qr_slug = await repo.create_station(
        operator_id, "live-test-station-uah", 10.0, mode="ocpp",
        ocpp_charge_point_id=f"CP-LIVE-UAH-{telegram_id}",
    )

    invoice_id = f"inv-live-{telegram_id}"
    id_tag = secrets.token_urlsafe(12)

    pool = await connection.get_db_pool()
    async with pool.acquire() as conn:
        reservation_id = await conn.fetchval("""
            INSERT INTO charging_reservations (
                operator_id, station_id, payment_method, reserved_uah,
                invoice_id, id_tag, status
            )
            VALUES ($1, $2, 'uah', $3, $4, $5, 'active')
            RETURNING id
        """, operator_id, station_id, "100.00", invoice_id, id_tag)

    yield operator_id, reservation_id

    pool = await connection.get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM charging_reservations WHERE operator_id = $1", operator_id)
        await conn.execute("DELETE FROM operator_stations WHERE operator_id = $1", operator_id)
        await conn.execute("DELETE FROM operators WHERE id = $1", operator_id)


async def test_claim_reservation_for_settlement_succeeds_once_on_real_postgres(live_active_uah_reservation):
    operator_id, reservation_id = live_active_uah_reservation

    claim = await repo.claim_reservation_for_settlement(operator_id, reservation_id)

    assert claim is not None
    assert claim["reserved_uah"] is not None

    pool = await connection.get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM charging_reservations WHERE id = $1", reservation_id,
        )
    assert row["status"] == "settling"


async def test_concurrent_claim_is_won_by_exactly_one_caller(live_active_uah_reservation):
    """
    ГОЛОВНИЙ ТЕСТ (рев'ю бандла 3c-ii): дві ПАРАЛЕЛЬНІ спроби claim на ту
    саму 'active' резервацію — обидві бачать статус 'active' до того, як
    будь-яка закомітить (жодного штучного затримання не потрібно: UPDATE
    ... WHERE status = 'active' на реальному Postgres сам серіалізує
    конкурентні UPDATE через рядковий лок). Рівно ОДИН виклик мав
    отримати dict, другий — None; після обох спроб резервація рівно ОДИН
    раз перейшла в 'settling', а не застрягла й не "подвоїлась".
    """
    operator_id, reservation_id = live_active_uah_reservation

    results = await asyncio.gather(
        repo.claim_reservation_for_settlement(operator_id, reservation_id),
        repo.claim_reservation_for_settlement(operator_id, reservation_id),
    )

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, "Рівно один паралельний виклик мав виграти claim"
    assert len(losers) == 1, "Другий мав побачити 0 рядків (RETURNING порожньо) і повернути None"

    pool = await connection.get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM charging_reservations WHERE id = $1", reservation_id,
        )
    assert row["status"] == "settling"


async def test_claim_on_already_settling_reservation_returns_none(live_active_uah_reservation):
    """Послідовний (не паралельний) повторний виклик — той самий мʼютекс, інша перевірка."""
    operator_id, reservation_id = live_active_uah_reservation

    first = await repo.claim_reservation_for_settlement(operator_id, reservation_id)
    second = await repo.claim_reservation_for_settlement(operator_id, reservation_id)

    assert first is not None
    assert second is None
