"""
Регресійний тест ПРОТИ РЕАЛЬНОГО Postgres: `alembic upgrade head` на
ПОРОЖНІЙ базі має давати робочу схему.

Чому цей тест існує. До міграції 0009 жодна міграція не створювала `users`
і `stations` — їх умів створити лише idempotent-бутстрап `create_tables()`.
При цьому `0012_wallet_topups` і `0016_charging_reservations` оголошують
`user_id ... REFERENCES users(user_id)`. Тобто ланцюг міграцій падав на
0012, і робоча база виникала виключно як побічний ефект старту застосунку.
`CLAUDE.md` §3 при цьому називає Alembic джерелом правди схеми.

Мокнути це неможливо в принципі: перевіряється не логіка Python, а те, чи
Postgres виконає DDL у тому порядку, в якому його подає Alembic. Єдиний
чесний доказ — підняти порожню базу й накотити.

Тест створює ОКРЕМУ тимчасову базу на тому ж сервері й видаляє її після
себе. База з DB_URL використовується лише для того, щоб виконати
CREATE DATABASE; її вміст не читається й не змінюється.

Потребує живого Postgres через DB_URL (той самий env var, що й
app/database/connection.py) — якщо не задано, тест пропускається.

Запуск локально:
    docker compose up -d postgres
    DB_URL=postgresql://<user>:<pass>@127.0.0.1:5432/<db> \\
        pytest test_alembic_from_scratch_live.py -v
"""
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL"), reason="потрібен живий Postgres (DB_URL) — див. докстрінг файлу",
)

REPO_ROOT = Path(__file__).parent

# Таблиці, без яких застосунок не працює. Перелік свідомо неповний: сенс
# не в тому, щоб дублювати схему в тесті (тоді тест доведеться правити
# кожною міграцією), а в тому, щоб зловити саме той клас поламки —
# «базових таблиць немає, бо їх створює тільки бутстрап».
REQUIRED_TABLES = (
    "users",
    "stations",
    "payments",
    "kw_transactions",
    "operators",
    "operator_stations",
    "operator_sessions",
    "wallet_topups",
    "charging_reservations",
)


def _swap_database(db_url: str, new_db: str) -> str:
    parts = urlsplit(db_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{new_db}", parts.query, parts.fragment))


@pytest.fixture
async def scratch_database():
    """
    Порожня база під один прогін. Ім'я випадкове, щоб паралельні прогони
    не побились за неї; прибирається у finally навіть коли тест упав, бо
    інакше кожен невдалий запуск лишав би сміття на сервері.
    """
    db_url = os.environ["DB_URL"]
    name = f"alembic_scratch_{secrets.token_hex(6)}"

    admin = await asyncpg.connect(db_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    except asyncpg.InsufficientPrivilegeError:
        await admin.close()
        pytest.skip("користувач DB_URL не має права CREATE DATABASE")
    await admin.close()

    try:
        yield _swap_database(db_url, name)
    finally:
        admin = await asyncpg.connect(db_url)
        # FORCE — щоб незакрите з'єднання від упалого тесту не лишало базу
        # невидаляною (PostgreSQL 13+).
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_alembic_upgrade_head_on_empty_database(scratch_database):
    """`alembic upgrade head` на порожній базі проходить і дає робочу схему."""
    env = {**os.environ, "DB_URL": scratch_database}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "alembic upgrade head впав на порожній базі\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )

    conn = await asyncpg.connect(scratch_database)
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        present = {r["tablename"] for r in rows}
        missing = [t for t in REQUIRED_TABLES if t not in present]
        assert not missing, f"після upgrade head немає таблиць: {missing}"
    finally:
        await conn.close()


async def test_foreign_keys_to_users_are_usable(scratch_database):
    """
    Схема не просто існує, а тримає той самий FK-ланцюг, на якому падав
    `upgrade head`: wallet_topups.user_id → users.user_id.

    Перевірка позитивна І негативна: вставка з існуючим користувачем
    проходить, з неіснуючим — відхиляється. Сам лише позитивний випадок
    пройшов би й на схемі, де зовнішнього ключа немає взагалі — а саме
    така розбіжність уже спостерігалась на проді
    (`ocpi_cdrs.user_id → users`, PROJECT_CONTEXT.md §7).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env={**os.environ, "DB_URL": scratch_database},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    conn = await asyncpg.connect(scratch_database)
    try:
        user_id = int.from_bytes(secrets.token_bytes(4), "big")
        await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, 0)", user_id)
        operator_id = await conn.fetchval(
            "INSERT INTO operators (name, telegram_id) VALUES ('alembic-scratch', $1) RETURNING id",
            user_id,
        )

        await conn.execute(
            """
            INSERT INTO wallet_topups (operator_id, user_id, invoice_id, package, kwh, amount_uah)
            VALUES ($1, $2, 'inv-scratch-ok', 'pack_50', 50, 750)
            """,
            operator_id, user_id,
        )

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                """
                INSERT INTO wallet_topups (operator_id, user_id, invoice_id, package, kwh, amount_uah)
                VALUES ($1, $2, 'inv-scratch-bad', 'pack_50', 50, 750)
                """,
                operator_id, user_id + 1,
            )
    finally:
        await conn.close()
