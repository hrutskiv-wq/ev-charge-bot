"""
Захардкоджений ваучер видалено — доказ ПРОТИ РЕАЛЬНОГО Postgres.

Чому не мок. `test_voucher_state_menu_bypass.py` перевіряє, який ТЕКСТ
відповів бот. Цього замало: текст «Невірний код ваучера» можна показати й
одночасно щось записати в базу. Тут перевіряється інше — що після введення
коду в `users` і `kw_transactions` **не змінилось нічого**. Фейкове
з'єднання ані транзакції, ані стану не має, тому такого твердження не
доводить у принципі.

Передісторія (`PROJECT_CONTEXT.md` §6.3). Код `VOLTie100` / `VOLT100`
нараховував 100 кВт·год прямим `UPDATE users` + `INSERT kw_transactions`,
тобто повз `update_user_balance()` — четверте й останнє порушення правила
«одна точка запису», і єдине, що лишалось відкритим після бандла
`orphan-balance-guard`. Без ідемпотентності: код можна було ввести
необмежену кількість разів.

Видалено, а не полагоджено, за підставою з прода (запит 21.08.2026): у
`kw_transactions` жодного рядка з `description LIKE '%ваучер%'`, усі шість
депозитів пояснені. Активацій не було ніколи — залишок тестового гачка,
не промокод у роздачі.

Потребує живого Postgres через DB_URL — якщо не задано, тест пропускається.

Запуск локально:
    docker compose up -d postgres
    DB_URL="postgresql://ev_admin:CHANGE_ME@127.0.0.1:5432/ev_charge_base" \\
        pytest test_voucher_removed_live.py -v
"""
import os
import secrets
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.database import connection
from app.handlers.user import process_text_voucher

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL"), reason="потрібен живий Postgres (DB_URL) — див. докстрінг файлу",
)

REPO_ROOT = Path(__file__).parent

# Обидва історичні написання. Перевіряються разом, бо в коді вони стояли в
# одному списку й прибирались одним рухом — окремий тест на кожен дав би
# хибне відчуття, що це різні шляхи.
FORMER_CODES = ("VOLTie100", "VOLT100")


def _swap_database(db_url: str, new_db: str) -> str:
    parts = urlsplit(db_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{new_db}", parts.query, parts.fragment))


@pytest.fixture
async def pooled_schema():
    """Порожня база зі схемою з Alembic + пул у глобалі `connection.db_pool`."""
    db_url = os.environ["DB_URL"]
    name = f"voucher_{secrets.token_hex(6)}"

    admin = await asyncpg.connect(db_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    except asyncpg.InsufficientPrivilegeError:
        await admin.close()
        pytest.skip("користувач DB_URL не має права CREATE DATABASE")
    await admin.close()

    url = _swap_database(db_url, name)
    original = connection.db_pool
    pool = None
    try:
        migrated = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT, env={**os.environ, "DB_URL": url},
            capture_output=True, text=True,
        )
        assert migrated.returncode == 0, f"alembic upgrade head впав:\n{migrated.stderr}"

        pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
        connection.db_pool = pool
        yield pool
    finally:
        connection.db_pool = original
        if pool is not None:
            await pool.close()
        admin = await asyncpg.connect(db_url)
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def _make_message(text: str, user_id: int):
    message = MagicMock()
    message.text = text
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.answer = AsyncMock()
    return message


@pytest.mark.parametrize("code", FORMER_CODES)
async def test_former_voucher_code_changes_nothing_in_database(pooled_schema, code):
    """
    ГОЛОВНЕ ТВЕРДЖЕННЯ: після введення колишнього коду баланс не змінився
    і рядка в журналі не з'явилось.

    Користувач створюється заздалегідь із нульовим балансом — інакше
    «рядка немає» було б неможливо відрізнити від «рядок є, але порожній».
    """
    user_id = int.from_bytes(secrets.token_bytes(5), "big")
    async with pooled_schema.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, 0)", user_id)

    message = _make_message(code, user_id)
    await process_text_voucher(message, AsyncMock())

    async with pooled_schema.acquire() as conn:
        balance = float(await conn.fetchval(
            "SELECT balance FROM users WHERE user_id = $1", user_id
        ))
        ledger_rows = await conn.fetchval(
            "SELECT count(*) FROM kw_transactions WHERE user_id = $1", user_id
        )

    assert balance == 0.0, f"{code} нарахував {balance} кВт·год — гілку не видалено"
    assert ledger_rows == 0, f"{code} лишив {ledger_rows} рядків у журналі"
    assert "Невірний код ваучера" in message.answer.call_args[0][0]


async def test_repeated_entry_still_changes_nothing(pooled_schema):
    """
    Десять вводів поспіль — нуль записів.

    Відсутність ідемпотентності була однією з трьох претензій до цієї гілки
    (`PROJECT_CONTEXT.md` §6.3): код можна було ввести необмежену кількість
    разів, і кожен раз нараховував. Після видалення питання ідемпотентності
    зникає разом із нарахуванням — але зафіксувати це варто саме прогоном,
    бо «нічого не робить N разів» — сильніше твердження, ніж «нічого не
    робить один раз».
    """
    user_id = int.from_bytes(secrets.token_bytes(5), "big")
    async with pooled_schema.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, 0)", user_id)

    for _ in range(10):
        await process_text_voucher(_make_message("VOLTie100", user_id), AsyncMock())

    async with pooled_schema.acquire() as conn:
        balance = float(await conn.fetchval(
            "SELECT balance FROM users WHERE user_id = $1", user_id
        ))
        ledger_rows = await conn.fetchval(
            "SELECT count(*) FROM kw_transactions WHERE user_id = $1", user_id
        )

    assert balance == 0.0
    assert ledger_rows == 0


async def test_menu_button_interception_is_untouched(pooled_schema):
    """
    Перехоплення кнопок меню лишилось працювати.

    Воно закривало окремий баг: хендлер із фільтром лише за станом ковтав
    БУДЬ-яке повідомлення, поки бот «чекав код», і на натискання «Баланс»
    відповідав «Невірний код ваучера». Видалення ваучера цю гілку не
    зачіпає, і саме тому її треба перевірити — щоб наступна сесія не
    прибрала «зайвий» код разом із мертвим.
    """
    user_id = int.from_bytes(secrets.token_bytes(5), "big")
    async with pooled_schema.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, 0)", user_id)

    message = _make_message("Баланс 💰", user_id)
    await process_text_voucher(message, AsyncMock())

    assert message.answer.called, "натискання кнопки меню лишилось без відповіді"
    assert "Невірний код ваучера" not in message.answer.call_args[0][0], (
        "кнопку меню знову трактовано як код ваучера — регресія бага, "
        "заради якого писався test_voucher_state_menu_bypass.py"
    )
