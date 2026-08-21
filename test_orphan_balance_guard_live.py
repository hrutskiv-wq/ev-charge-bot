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


@pytest.fixture
async def schema_before_fk(fresh_schema):
    """
    Схема на 0019 — ДО зовнішнього ключа `kw_transactions.user_id`.

    Потрібна тестам, вихідний стан яких — осиротілий рядок журналу: після
    0020 такий рядок фізично не вставити, у цьому й сенс ключа. Відкат
    робиться самим Alembic, а не ручним DROP CONSTRAINT, — щоб перевірявся
    той самий downgrade, яким користуватиметься людина.
    """
    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0019_add_telegram_provider"],
        cwd=REPO_ROOT, env={**os.environ, "DB_URL": fresh_schema},
        capture_output=True, text=True,
    )
    assert down.returncode == 0, f"downgrade на 0019 впав:\n{down.stderr}"
    yield fresh_schema


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


async def test_correction_requires_existing_account(pooled):
    """
    Гілка correction рахунку НЕ створює: коригувати можна лише наявне.
    Контракт той самий, що на дебетних гілках — BalanceRowMissing і чистий
    журнал.
    """
    user_id = _new_user_id()

    with pytest.raises(BalanceRowMissing):
        await update_user_balance(user_id, -50.0, t_type="correction")

    async with pooled.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM kw_transactions WHERE user_id = $1", user_id
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM users WHERE user_id = $1", user_id
        ) == 0


async def test_correction_amount_is_signed_both_ways(pooled):
    """
    Знак приходить ЗЗОВНІ, функція його не вигадує: correction(-N) списує,
    correction(+N) нараховує. Це єдина знакова гілка, і саме тому переплутати
    її з рештою (які беруть модуль) — дорого.

    Тип у журналі має бути 'correction', а не 'withdrawal': до 21.08.2026
    його не було в enum-розгалуженні функції, і correction мовчки лягав
    у загальний else саме як 'withdrawal'.
    """
    user_id = _new_user_id()
    await update_user_balance(user_id, 100.0, t_type="deposit")

    await update_user_balance(user_id, -30.0, t_type="correction", description="мінус")
    await update_user_balance(user_id, 5.0, t_type="correction", description="плюс")

    async with pooled.acquire() as conn:
        balance = float(await conn.fetchval(
            "SELECT balance FROM users WHERE user_id = $1", user_id
        ))
        ledger = float(await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM kw_transactions WHERE user_id = $1", user_id
        ))
        amounts = [float(r["amount"]) for r in await conn.fetch(
            "SELECT amount FROM kw_transactions WHERE user_id = $1 AND type = 'correction' "
            "ORDER BY id", user_id
        )]

    assert balance == 75.0, "100 - 30 + 5"
    assert ledger == balance, "журнал розійшовся з балансом"
    assert amounts == [-30.0, 5.0], "знак не збережено як переданий"


async def test_repair_script_default_keeps_ledger_balance(schema_before_fk):
    """
    Дефолт скрипта — створити рахунок із сумою журналу, НЕ занулювати.
    Для справжнього осиротілого користувача занулення знищило б реальні
    кВт·год, тому мовчазним воно бути не може.
    """
    orphan_id = _new_user_id()
    conn = await asyncpg.connect(schema_before_fk)
    try:
        await conn.execute(
            "INSERT INTO kw_transactions (user_id, type, amount, description) "
            "VALUES ($1, 'deposit', 42.00, 'справжній осиротілий користувач')",
            orphan_id,
        )
    finally:
        await conn.close()

    done = subprocess.run(
        [sys.executable, "scripts/repair_orphan_balance.py", str(orphan_id), "--apply"],
        cwd=REPO_ROOT, env={**os.environ, "DB_URL": schema_before_fk},
        capture_output=True, text=True,
    )
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"

    conn = await asyncpg.connect(schema_before_fk)
    try:
        assert float(await conn.fetchval(
            "SELECT balance FROM users WHERE user_id = $1", orphan_id
        )) == 42.0, "дефолт занулив баланс — реальні кВт·год загинули б"
        assert await conn.fetchval(
            "SELECT count(*) FROM kw_transactions WHERE user_id = $1", orphan_id
        ) == 1, "дефолт дописав рядок у журнал, хоч не мав"
    finally:
        await conn.close()


async def test_repair_script_zero_out_appends_correction(schema_before_fk):
    """
    `--zero-out` створює рахунок І зануляє його коригувальним рядком.
    Початковий рядок журналу НЕ зникає — append-only: слід помилки має
    лишитись видимим назавжди.
    """
    orphan_id = _new_user_id()
    reason = "ID самого бота, нараховано помилково видаленим вебхуком"
    conn = await asyncpg.connect(schema_before_fk)
    try:
        await conn.execute(
            "INSERT INTO kw_transactions (user_id, type, amount, description) "
            "VALUES ($1, 'deposit', 50.00, 'Успішна оплата пакету: 50.0 кВт·год через Банку Monobank')",
            orphan_id,
        )
    finally:
        await conn.close()

    done = subprocess.run(
        [sys.executable, "scripts/repair_orphan_balance.py", str(orphan_id),
         "--apply", "--zero-out", reason],
        cwd=REPO_ROOT, env={**os.environ, "DB_URL": schema_before_fk},
        capture_output=True, text=True,
    )
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"

    conn = await asyncpg.connect(schema_before_fk)
    try:
        assert float(await conn.fetchval(
            "SELECT balance FROM users WHERE user_id = $1", orphan_id
        )) == 0.0, "фантомний баланс лишився"
        rows = await conn.fetch(
            "SELECT type::text AS t, amount, description FROM kw_transactions "
            "WHERE user_id = $1 ORDER BY id", orphan_id
        )
        assert len(rows) == 2, "початковий рядок мав ЛИШИТИСЬ, а коригувальний — додатись"
        assert rows[0]["t"] == "deposit" and float(rows[0]["amount"]) == 50.0
        assert rows[1]["t"] == "correction" and float(rows[1]["amount"]) == -50.0
        assert reason in rows[1]["description"], (
            "причина не потрапила в журнал — а це єдине місце, де вона лишиться"
        )
        assert float(await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM kw_transactions WHERE user_id = $1", orphan_id
        )) == 0.0
    finally:
        await conn.close()


async def test_fk_migration_fails_on_orphan_and_passes_after_repair(fresh_schema):
    """
    Порядок «спершу залатати, потім FK» — доведений, а не заявлений.

    На схемі 0019 (до FK) вставляємо осиротілий рядок журналу, потім
    пробуємо накотити 0020: має впасти. Далі проганяємо
    scripts/repair_orphan_balance.py --apply і накочуємо знову: має пройти.

    Це і є вплив FK на наявні дані: `ADD CONSTRAINT` без `NOT VALID`
    перевіряє всі наявні рядки негайно, тому один осиротілий user_id
    зупиняє міграцію.
    """
    downgraded = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0019_add_telegram_provider"],
        cwd=REPO_ROOT, env={**os.environ, "DB_URL": fresh_schema},
        capture_output=True, text=True,
    )
    assert downgraded.returncode == 0, downgraded.stderr

    orphan_id = _new_user_id()
    conn = await asyncpg.connect(fresh_schema)
    try:
        await conn.execute(
            """
            INSERT INTO kw_transactions (user_id, type, amount, description)
            VALUES ($1, 'deposit', 50.00, 'осиротілий рядок для тесту')
            """,
            orphan_id,
        )
    finally:
        await conn.close()

    env = {**os.environ, "DB_URL": fresh_schema}

    failed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert failed.returncode != 0, (
        "0020 накотилась попри осиротілий рядок — FK не перевіряє наявні дані"
    )
    assert "ForeignKeyViolation" in failed.stderr or "foreign key" in failed.stderr.lower(), (
        f"впало не на зовнішньому ключі:\n{failed.stderr[-800:]}"
    )

    repaired = subprocess.run(
        [sys.executable, "scripts/repair_orphan_balance.py", str(orphan_id), "--apply"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert repaired.returncode == 0, f"ремонт не спрацював:\n{repaired.stdout}\n{repaired.stderr}"

    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert migrated.returncode == 0, (
        f"після ремонту 0020 все одно не накотилась:\n{migrated.stderr[-800:]}"
    )


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
