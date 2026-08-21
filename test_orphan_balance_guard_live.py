"""
Запобіжник осиротілого балансу — ПРОТИ РЕАЛЬНОГО Postgres.

Чому не мок. `test_balance.py` підміняє з'єднання фейком і перевіряє, ЯКІ
запити було викликано. Тут перевіряється інше: що після відкоту транзакції
в базі НЕ ЛИШИЛОСЬ рядка журналу. Фейкове з'єднання не має ні транзакцій,
ні відкоту — воно за побудовою не може цього довести.

Передісторія (`docs/plan-orphan-balance-guard.md`): 15.07.2026 у проді
з'явився рядок `kw_transactions` на +50 кВт·год для `user_id`, якого немає
в `users`. Записав його не `update_user_balance()`, а вебхук Monobank
(`app/api/payments.py`, видалений 28.07.2026), який писав повз єдину точку
входу. Ці тести закривають не той інцидент, а клас: щоб сама єдина точка
входу ніколи не залишила журнал із записом про неіснуючий рахунок.

Потребує живого Postgres через DB_URL — якщо не задано, тест пропускається.

Запуск локально:
    docker compose up -d postgres
    BOT_TOKEN="123456:ci-placeholder-bot-token" \\
    GEMINI_API_KEY="ci-placeholder-gemini-key" \\
    OCPI_SECRET_TOKEN="ci-placeholder-token" \\
    DB_URL="postgresql://<user>:<pass>@127.0.0.1:5432/<db>" \\
        pytest test_orphan_balance_guard_live.py -v
"""
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.database import connection
from app.database.connection import BalanceRowMissing, update_user_balance

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL"), reason="потрібен живий Postgres (DB_URL) — див. докстрінг файлу",
)

REPO_ROOT = Path(__file__).parent


def _swap_database(db_url: str, new_db: str) -> str:
    parts = urlsplit(db_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{new_db}", parts.query, parts.fragment))


@pytest.fixture
async def fresh_schema():
    """
    Окрема порожня база зі схемою з Alembic. Не чіпає базу з DB_URL: тести
    пишуть у `users` і `kw_transactions`, і робити це в спільній базі
    означало б залежати від того, що там уже лежить.
    """
    db_url = os.environ["DB_URL"]
    name = f"guard_{secrets.token_hex(6)}"

    admin = await asyncpg.connect(db_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    except asyncpg.InsufficientPrivilegeError:
        await admin.close()
        pytest.skip("користувач DB_URL не має права CREATE DATABASE")
    await admin.close()

    url = _swap_database(db_url, name)
    try:
        migrated = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT, env={**os.environ, "DB_URL": url},
            capture_output=True, text=True,
        )
        assert migrated.returncode == 0, f"alembic upgrade head впав:\n{migrated.stderr}"
        yield url
    finally:
        admin = await asyncpg.connect(db_url)
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def pooled(fresh_schema):
    """
    Пул на тимчасову базу, підставлений у глобал `connection.db_pool`, бо
    update_user_balance() без явного conn бере пул саме звідти. Оригінал
    відновлюється — інакше наступні тести в тій самій сесії pytest пішли б
    у чужу базу.
    """
    original = connection.db_pool
    pool = await asyncpg.create_pool(fresh_schema, min_size=1, max_size=3)
    connection.db_pool = pool
    try:
        yield pool
    finally:
        connection.db_pool = original
        await pool.close()


def _new_user_id() -> int:
    return int.from_bytes(secrets.token_bytes(5), "big")


async def test_debit_of_missing_user_writes_nothing(pooled):
    """
    ГОЛОВНЕ ТВЕРДЖЕННЯ: при відсутньому користувачі дебет падає, журнал НЕ
    поповнюється і від'ємний баланс НЕ створюється.

    Перевірка — трьома запитами до бази ПІСЛЯ винятку, а не за викликами.
    До цієї правки тест провалився б у зворотний бік: виклик проходив,
    створювався рядок `users` з balance = -25.00 і рядок журналу.
    """
    user_id = _new_user_id()

    with pytest.raises(BalanceRowMissing):
        await update_user_balance(user_id, 25.0, t_type="ocpi_session")

    async with pooled.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM kw_transactions WHERE user_id = $1", user_id
        ) == 0, "рядок журналу лишився після відкоту — саме це й є осиротілий баланс"
        assert await conn.fetchval(
            "SELECT count(*) FROM users WHERE user_id = $1", user_id
        ) == 0, "дебет створив рахунок, якого не мав створювати"


async def test_hold_of_missing_user_writes_nothing(pooled):
    """Друга дебетна гілка — та сама вимога. `hold` має свій шлях у коді."""
    user_id = _new_user_id()

    with pytest.raises(BalanceRowMissing):
        await update_user_balance(user_id, 10.0, t_type="hold")

    async with pooled.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM kw_transactions WHERE user_id = $1", user_id
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM users WHERE user_id = $1", user_id
        ) == 0


async def test_credit_creates_account_and_keeps_invariant(pooled):
    """
    Кредит новому користувачеві ЗАВОДИТЬ рахунок — відмова не поширилась на
    кредитні гілки. Гроші вже прийняті, відмовляти в зарахуванні не можна.
    Інваріант `balance == SUM(amount)` перевіряється по факту, а не
    припускається.
    """
    user_id = _new_user_id()

    assert await update_user_balance(user_id, 50.0, t_type="deposit") is True

    async with pooled.acquire() as conn:
        balance = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id)
        ledger = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM kw_transactions WHERE user_id = $1", user_id
        )
        assert balance is not None, "кредит не створив рахунок"
        assert float(balance) == 50.0
        assert float(ledger) == 50.0, "журнал розійшовся з балансом"


async def test_hold_without_funds_still_returns_false(pooled):
    """
    Нестача коштів лишилась `False`, а не стала винятком. Це різні події:
    False — очікуваний бізнес-результат, який викликач обробляє; виняток —
    баг. Якби вони злились, резервація тихо не створювалась би замість того,
    щоб голосно впасти.
    """
    user_id = _new_user_id()
    await update_user_balance(user_id, 5.0, t_type="deposit")

    assert await update_user_balance(user_id, 10.0, t_type="hold") is False

    async with pooled.acquire() as conn:
        assert float(await conn.fetchval(
            "SELECT balance FROM users WHERE user_id = $1", user_id
        )) == 5.0, "невдалий hold змінив баланс"
        assert await conn.fetchval(
            "SELECT count(*) FROM kw_transactions WHERE user_id = $1 AND type = 'hold'", user_id
        ) == 0, "невдалий hold лишив запис у журналі"
