"""
Порівняння двох схем ПРОТИ РЕАЛЬНОГО Postgres: база, піднята `alembic upgrade
head`, проти бази, піднятої idempotent-бутстрапом.

Чому цей тест існує. Схему описано у двох місцях (`CLAUDE.md` §3): Alembic і
`create_tables()` / `init_ocpi_tables()` / `init_operator_tables()`. Правило
«при зміні схеми оновлюй обидва» трималось на увазі людини — і не втрималось
**чотири рази**: `refund` (0008), відсутній FK `ocpi_cdrs.user_id → users`
(спостережено на проді 06.08.2026), `payment_provider` без `telegram` (0019),
`users`/`stations` не створювала жодна міграція (0009). Останні два розходження
з переліку `PROJECT_CONTEXT.md` §7 знайдено руками 20.08.2026 — тобто сам
перелік, складений уважно, теж виявився неповним.

**Базлайн, а не «нуль розходжень».** На момент написання схеми розходяться
у 18 місцях і 5 таблиць існують лише в бутстрапі. Тест, який вимагав би повної
рівності, був би червоним з першого дня — і його вимкнули б за тиждень. Тому
відомі розходження зафіксовані нижче списком, а тест падає на **новому**.
Він падає й тоді, коли розходження ЗНИКЛО, а зі списку його не прибрали:
інакше список тихо загниє й перестане описувати дійсність — рівно та хвороба,
проти якої він і пишеться.

**Чого тест НЕ порівнює:** індекси. Їхні набори теж різні (`PROJECT_CONTEXT.md`
§7 п.6), але імена індексів у двох механізмах різні за побудовою, і порівняння
за іменем дало б шум замість сигналу. Порівнювати за (таблиця, колонки) —
окрема робота; поки цього немає, не вважати, що індекси покриті.

Потребує живого Postgres через DB_URL — якщо не задано, тест пропускається.

Запуск локально:
    docker compose up -d postgres
    DB_URL=postgresql://<user>:<pass>@127.0.0.1:5432/<db> \\
        pytest test_schema_parity_live.py -v
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

# Бутстрап у ОКРЕМОМУ процесі — свідомо, а не викликом у поточному: обидві
# init_*_tables() беруть пул через глобальний connection.db_pool, і підміна
# цього глобала посеред сесії pytest зачепила б інші тести.
_BOOTSTRAP_SCRIPT = """
import asyncio
from app.database import connection
from app.database.ocpi_repo import init_ocpi_tables
from app.database.operators_repo import init_operator_tables

async def main():
    await connection.init_postgres()      # створює пул і викликає create_tables()
    await init_ocpi_tables()
    await init_operator_tables()
    await connection.close_postgres()

asyncio.run(main())
"""

# П'ять таблиць OCPI, яких Alembic не створює взагалі (PROJECT_CONTEXT.md §7 п.3:
# міграція 0007 порожня). Вони виключені з поколонкового порівняння — інакше
# 38 записів шуму сховали б справжній сигнал — і перевіряються окремо.
BOOTSTRAP_ONLY_TABLES = frozenset({
    "ocpi_locations", "ocpi_evses", "ocpi_connectors", "ocpi_sessions", "ocpi_tariffs",
})

# Відомі розходження станом на 21.08.2026, зняті виміром, а не з документа.
# Префікс A — є лише в схемі Alembic, B — лише в схемі бутстрапу.
KNOWN_DIFFERENCES = frozenset({
    # -- Ширина числових полів. Бутстрап ширший; практично не стріляло, бо
    #    8,2 це 999 999,99 кВт·год на транзакцію.
    "A col kw_transactions.amount numeric(8,2,-1) null=NO",
    "B col kw_transactions.amount numeric(10,2,-1) null=NO",
    "A col ocpi_cdrs.total_energy numeric(8,2,-1) null=NO",
    "B col ocpi_cdrs.total_energy numeric(10,4,-1) null=NO",

    # -- Обов'язковість. Alembic суворіший скрізь, крім raw_payload навпаки.
    "A col ocpi_cdrs.raw_payload jsonb(-1,-1,-1) null=NO",
    "B col ocpi_cdrs.raw_payload jsonb(-1,-1,-1) null=YES",
    "A col payments.status USER-DEFINED(-1,-1,-1) null=NO",
    "B col payments.status USER-DEFINED(-1,-1,-1) null=YES",
    "A col payments.user_id bigint(64,0,-1) null=NO",
    "B col payments.user_id bigint(64,0,-1) null=YES",
    "A col kw_transactions.user_id bigint(64,0,-1) null=NO",
    "B col kw_transactions.user_id bigint(64,0,-1) null=YES",
    "A col ocpi_cdrs.user_id bigint(64,0,-1) null=NO",
    "B col ocpi_cdrs.user_id bigint(64,0,-1) null=YES",

    # -- Зовнішні ключі на users. У схемі Alembic їх спершу не було ЖОДНОГО.
    #    Це пояснює спостереження на проді 06.08.2026 (`CLAUDE.md` §6a):
    #    "FK ocpi_cdrs.user_id → users(user_id) у проді не діє" — таблиця
    #    приїхала з початкової міграції, де ключа ніколи не було, а не
    #    зіпсувалась пізніше.
    #
    #    `kw_transactions.user_id` звідси ПРИБРАНО 21.08.2026: міграція 0020
    #    додала ключ в Alembic-гілку, розходження зникло. Рядок прибрано тим
    #    самим комітом, що й міграція — інакше `main` червоний в один бік
    #    або в інший (див. другу перевірку test_no_new_schema_divergence).
    #    Два, що лишились, — наступний пункт боргу.
    "B fk payments.user_id -> users.user_id",
    "B fk ocpi_cdrs.user_id -> users.user_id",

    # -- ENUM. 0019 додав 'telegram' в Alembic-гілку, але 'liqpay' лишився
    #    тільки в ній: схеми розійшлись в обидва боки, а не одна відстала.
    "A enum payment_provider=liqpay",
})

_COLUMNS_SQL = """
    SELECT table_name, column_name, data_type,
           COALESCE(numeric_precision, -1) AS p,
           COALESCE(numeric_scale, -1) AS s,
           COALESCE(character_maximum_length, -1) AS len,
           is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name <> 'alembic_version'
"""

_FK_SQL = """
    SELECT tc.table_name, kcu.column_name,
           ccu.table_name AS ref_table, ccu.column_name AS ref_col
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
    JOIN information_schema.constraint_column_usage ccu
      ON ccu.constraint_name = tc.constraint_name
    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
"""

_ENUM_SQL = """
    SELECT t.typname, e.enumlabel
    FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
"""


def _swap_database(db_url: str, new_db: str) -> str:
    parts = urlsplit(db_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{new_db}", parts.query, parts.fragment))


async def _snapshot(url: str, skip_tables: frozenset) -> set:
    conn = await asyncpg.connect(url)
    try:
        cols = await conn.fetch(_COLUMNS_SQL)
        fks = await conn.fetch(_FK_SQL)
        enums = await conn.fetch(_ENUM_SQL)
    finally:
        await conn.close()

    out = set()
    for r in cols:
        if r["table_name"] in skip_tables:
            continue
        out.add(
            f"col {r['table_name']}.{r['column_name']} "
            f"{r['data_type']}({r['p']},{r['s']},{r['len']}) null={r['is_nullable']}"
        )
    for r in fks:
        if r["table_name"] in skip_tables:
            continue
        out.add(f"fk {r['table_name']}.{r['column_name']} -> {r['ref_table']}.{r['ref_col']}")
    for r in enums:
        out.add(f"enum {r['typname']}={r['enumlabel']}")
    return out


async def _table_names(url: str) -> set:
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    finally:
        await conn.close()
    return {r["tablename"] for r in rows} - {"alembic_version"}


@pytest.fixture
async def two_schemas():
    """
    Дві порожні бази: одна піднята Alembic, друга — бутстрапом. Обидві
    прибираються у finally, щоб невдалий прогін не лишав сміття на сервері.
    """
    db_url = os.environ["DB_URL"]
    suffix = secrets.token_hex(6)
    name_a, name_b = f"parity_alembic_{suffix}", f"parity_bootstrap_{suffix}"

    admin = await asyncpg.connect(db_url)
    try:
        for name in (name_a, name_b):
            await admin.execute(f'CREATE DATABASE "{name}"')
    except asyncpg.InsufficientPrivilegeError:
        await admin.close()
        pytest.skip("користувач DB_URL не має права CREATE DATABASE")
    await admin.close()

    url_a, url_b = _swap_database(db_url, name_a), _swap_database(db_url, name_b)
    try:
        alembic = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT, env={**os.environ, "DB_URL": url_a},
            capture_output=True, text=True,
        )
        assert alembic.returncode == 0, f"alembic upgrade head впав:\n{alembic.stderr}"

        bootstrap = subprocess.run(
            [sys.executable, "-c", _BOOTSTRAP_SCRIPT],
            cwd=REPO_ROOT, env={**os.environ, "DB_URL": url_b},
            capture_output=True, text=True,
        )
        assert bootstrap.returncode == 0, f"бутстрап впав:\n{bootstrap.stderr}"

        yield url_a, url_b
    finally:
        admin = await asyncpg.connect(db_url)
        for name in (name_a, name_b):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_no_new_schema_divergence(two_schemas):
    """Схеми розходяться рівно там, де це вже відомо й записано."""
    url_a, url_b = two_schemas
    snap_a = await _snapshot(url_a, BOOTSTRAP_ONLY_TABLES)
    snap_b = await _snapshot(url_b, BOOTSTRAP_ONLY_TABLES)

    diff = {f"A {x}" for x in snap_a - snap_b} | {f"B {x}" for x in snap_b - snap_a}

    new = sorted(diff - KNOWN_DIFFERENCES)
    assert not new, (
        "З'явилось розходження між Alembic і бутстрапом, якого немає в KNOWN_DIFFERENCES.\n"
        "Це саме те, заради чого тест написаний: схему змінили в одному місці з двох.\n"
        "Полагодьте розходження або — якщо воно свідоме — додайте рядок у список\n"
        "з поясненням ЧОМУ.\n\n" + "\n".join(f"  {x}" for x in new)
    )

    gone = sorted(KNOWN_DIFFERENCES - diff)
    assert not gone, (
        "Розходження зникло, а з KNOWN_DIFFERENCES його не прибрали. Список має\n"
        "описувати дійсність, інакше він тихо загниє — приберіть ці рядки.\n\n"
        + "\n".join(f"  {x}" for x in gone)
    )


async def test_ocpi_tables_exist_only_in_bootstrap(two_schemas):
    """
    П'ять таблиць OCPI створює лише бутстрап (§7 п.3 — міграція 0007 порожня).

    Перевірка тримає це явним фактом, а не мовчазним винятком у попередньому
    тесті: якщо колись ці таблиці додадуть у міграцію (або приберуть із
    бутстрапу), тест впаде й змусить прибрати виняток замість того, щоб
    порівняння тихо працювало на застарілому списку.
    """
    url_a, url_b = two_schemas
    tables_a, tables_b = await _table_names(url_a), await _table_names(url_b)

    assert BOOTSTRAP_ONLY_TABLES <= tables_b, (
        f"бутстрап не створив: {sorted(BOOTSTRAP_ONLY_TABLES - tables_b)}"
    )
    assert not (BOOTSTRAP_ONLY_TABLES & tables_a), (
        "Alembic почав створювати таблиці, які були виключені з порівняння як "
        f"«тільки бутстрап»: {sorted(BOOTSTRAP_ONLY_TABLES & tables_a)}. "
        "Приберіть їх із BOOTSTRAP_ONLY_TABLES."
    )
