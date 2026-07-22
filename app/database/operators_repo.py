"""
Репозиторій White-Label білінгу: тенанти (оператори зарядних станцій),
їхні станції, сесії зарядки й журнал розрахунків.

ГОЛОВНЕ ПРАВИЛО МОДУЛЯ — МУЛЬТИТЕНАНТНІСТЬ
Кожна таблиця має operator_id, і кожна функція, що читає або змінює дані
оператора, приймає operator_id ПЕРШИМ аргументом і обовʼязково має його у
WHERE. Оператор А фізично не може отримати рядок оператора Б навіть при
підстановці чужого id — запит просто поверне порожній результат.

Єдиний свідомий виняток — get_station_by_qr_slug(): це публічний QR-флоу
водія, де сам slug і є секретом (сторінка /s/{qr_slug}), водій не
автентифікований і operator_id взяти нізвідки. Функція повертає operator_id
станції, щоб уся подальша робота знову йшла тенант-скоупнутими викликами.

Другий, службовий виняток — get_station_by_ocpp_charge_point_id(): станція
підключається до OCPP-сервера по своєму charge_point_id (websocket), теж до
будь-якої автентифікації оператора. Так само повертає operator_id.

Третій виняток — list_public_stations_near() (Промпт 4c): пошук найближчих
станцій для водія в Telegram-боті. Водій так само не автентифікований і не
"свій" жодному оператору, тож фільтр тут не operator_id, а географічний
радіус + активність станції/оператора. Кожен рядок так само несе
operator_id, щоб подальший перехід на /s/{qr_slug} знову йшов
тенант-скоупнутим шляхом.

Четвертий виняток — list_operators() (Промпт 5): звірка reconcile_operators.py
за своєю природою мусить пройтись по ВСІХ операторах, а не по одному
тенанту — це адмінський/cron-інструмент, а не запит від імені оператора.
Секретів (monobank_token_encrypted) не повертає — той, як і завжди,
дістається окремо через get_operator_monobank_token_encrypted(operator_id)
для кожного оператора з цього списку.

Схема продубльована в migrations/versions/0010_white_label_tenants.py —
при зміні оновлювати ОБИДВА місця (конвенція проєкту, див. PROJECT_CONTEXT.md).
"""
import logging
import secrets
from decimal import Decimal, ROUND_HALF_UP

from app.database.connection import get_db_pool
from app.services.geo import haversine_km

logger = logging.getLogger(__name__)

# Скільки байт ентропії в QR-слазі. token_urlsafe(12) → 16 символів,
# ~72 біти — вгадати чужу станцію перебором нереально, а надрукований під
# QR-кодом рядок лишається коротким.
QR_SLUG_BYTES = 12


def generate_qr_slug() -> str:
    """Генерує публічний slug станції для сторінки оплати /s/{qr_slug}."""
    return secrets.token_urlsafe(QR_SLUG_BYTES)


async def init_operator_tables():
    """
    Idempotent-дзеркало міграції 0010 — щоб застосунок піднявся на чистій
    базі без ручного `alembic upgrade head` (та сама конвенція, що й
    create_tables() та init_ocpi_tables()).
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # 1. Оператори (тенанти)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS operators (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            phone VARCHAR(32),
            telegram_id BIGINT NOT NULL UNIQUE,
            status VARCHAR(20) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'active', 'suspended')),
            commission_pct NUMERIC(5, 2) NOT NULL DEFAULT 4
                CHECK (commission_pct >= 0 AND commission_pct <= 100),
            monobank_token_encrypted TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 2. Станції оператора
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS operator_stations (
            id SERIAL PRIMARY KEY,
            operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            address TEXT,
            lat NUMERIC(9, 6),
            lng NUMERIC(9, 6),
            connector_type VARCHAR(50),
            power_kw NUMERIC(6, 2),
            mode VARCHAR(10) NOT NULL DEFAULT 'manual'
                CHECK (mode IN ('manual', 'ocpp')),
            ocpp_charge_point_id VARCHAR(100) UNIQUE,
            tariff_uah_kwh NUMERIC(10, 2) NOT NULL CHECK (tariff_uah_kwh >= 0),
            tariff_uah_start NUMERIC(10, 2) CHECK (tariff_uah_start >= 0),
            qr_slug VARCHAR(32) NOT NULL UNIQUE,
            status VARCHAR(20) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'offline', 'disabled')),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (id, operator_id)
        );
        """)

        # 2b. Платежі водіїв через еквайринг оператора (міграція 0011).
        # Створюється ДО operator_sessions, бо sessions.payment_id на неї
        # посилається.
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS operator_payments (
            id SERIAL PRIMARY KEY,
            operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
            invoice_id VARCHAR(100) NOT NULL,
            amount_uah NUMERIC(12, 2) NOT NULL CHECK (amount_uah >= 0),
            status VARCHAR(20) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'success', 'failed', 'expired', 'reversed')),
            payload JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (operator_id, invoice_id)
        );
        """)

        # 3. Сесії зарядки (operator_id денормалізовано + композитний FK)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS operator_sessions (
            id SERIAL PRIMARY KEY,
            operator_id INTEGER NOT NULL,
            station_id INTEGER NOT NULL,
            started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP WITH TIME ZONE,
            kwh NUMERIC(10, 3) CHECK (kwh >= 0),
            amount_uah NUMERIC(12, 2) CHECK (amount_uah >= 0),
            payment_id INTEGER REFERENCES operator_payments(id) ON DELETE SET NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'paid', 'charging', 'completed', 'failed', 'refunded')),
            driver_contact VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (station_id, operator_id)
                REFERENCES operator_stations (id, operator_id) ON DELETE CASCADE
        );
        """)

        # 4. Журнал розрахунків (знакова сума, без кешованого балансу)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS operator_payout_ledger (
            id SERIAL PRIMARY KEY,
            operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES operator_sessions(id) ON DELETE SET NULL,
            type VARCHAR(24) NOT NULL
                CHECK (type IN ('session_income', 'platform_commission',
                                'subscription_fee', 'payout', 'adjustment')),
            amount_uah NUMERIC(12, 2) NOT NULL,
            description TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 5. Індекси
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_operator_stations_operator ON operator_stations(operator_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_operator_sessions_operator_started ON operator_sessions(operator_id, started_at DESC);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_operator_sessions_station ON operator_sessions(station_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_operator_sessions_payment ON operator_sessions(payment_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_operator_ledger_operator_created ON operator_payout_ledger(operator_id, created_at DESC);")

        # 6. Ідемпотентність розрахунків (пояснення — в міграції 0010)
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_session_income ON operator_payout_ledger(session_id, type) WHERE session_id IS NOT NULL AND type IN ('session_income', 'platform_commission');")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_payment ON operator_sessions(payment_id) WHERE payment_id IS NOT NULL;")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_operator_payments_operator_created ON operator_payments(operator_id, created_at DESC);")

        # 7. Перенаправлення operator_sessions.payment_id з payments на
        # operator_payments (міграція 0011). CREATE TABLE IF NOT EXISTS вище
        # не чіпає вже створену таблицю, тому на базі, піднятій до 0011,
        # обмеження треба перевісити явно — інакше запис платежу водія
        # впаде на FK, який дивиться в чужу таблицю.
        await conn.execute("ALTER TABLE operator_sessions DROP CONSTRAINT IF EXISTS operator_sessions_payment_id_fkey;")
        await conn.execute("""
        ALTER TABLE operator_sessions
            ADD CONSTRAINT operator_sessions_payment_id_fkey
            FOREIGN KEY (payment_id) REFERENCES operator_payments(id) ON DELETE SET NULL;
        """)
        logger.info("🏷️ Таблиці White-Label білінгу (оператори/станції/сесії/журнал) верифіковано.")


# ---------------------------------------------------------------------------
# Оператори (тенанти)
# ---------------------------------------------------------------------------

# Явний перелік полів замість SELECT * — щоб monobank_token_encrypted
# ніколи не потрапив у звичайну вибірку, лог чи дамп кабінету випадково.
_OPERATOR_FIELDS = "id, name, phone, telegram_id, status, commission_pct, created_at"


async def create_operator(name: str, telegram_id: int, phone: str = None,
                          commission_pct: float = 4):
    """Створює оператора. Повертає його id, або None якщо telegram_id уже зайнятий."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            INSERT INTO operators (name, phone, telegram_id, commission_pct)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO NOTHING
            RETURNING id
        """, name, phone, telegram_id, commission_pct)


async def get_operator(operator_id: int):
    """Картка оператора БЕЗ еквайринг-токена."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            f"SELECT {_OPERATOR_FIELDS} FROM operators WHERE id = $1", operator_id
        )


async def get_operator_by_telegram_id(telegram_id: int):
    """Вхідна точка кабінету: за telegram_id знаходимо тенанта."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            f"SELECT {_OPERATOR_FIELDS} FROM operators WHERE telegram_id = $1", telegram_id
        )


async def set_operator_status(operator_id: int, status: str):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE operators SET status = $2 WHERE id = $1", operator_id, status
        )
        return result.endswith("1")


async def set_operator_monobank_token(operator_id: int, token_encrypted: str):
    """
    Зберігає ВЖЕ зашифрований токен еквайрингу оператора. Шифрування
    (Fernet на ENCRYPTION_KEY) робиться шаром вище — репозиторій навмисно
    не бачить відкритого токена, щоб той не міг потрапити сюди в лог.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE operators SET monobank_token_encrypted = $2 WHERE id = $1",
            operator_id, token_encrypted,
        )
        return result.endswith("1")


async def get_operator_monobank_token_encrypted(operator_id: int):
    """
    Окрема функція саме тому, що це секрет: щоб дістати токен, треба
    свідомо викликати її, а не отримати «безкоштовно» разом з get_operator().
    Результат НЕ логувати.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT monobank_token_encrypted FROM operators WHERE id = $1", operator_id
        )


async def list_operators():
    """
    ЄДИНИЙ виклик без operator_id, що повертає рядки БІЛЬШЕ ніж одного
    тенанта — четвертий свідомий виняток з правила ізоляції, див. докстрінг
    модуля. Для звірки (reconcile_operators.py, Промпт 5): кожен оператор
    зі списку далі обробляється своїми тенант-скоупними викликами.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(f"SELECT {_OPERATOR_FIELDS} FROM operators ORDER BY id")


# ---------------------------------------------------------------------------
# Станції
# ---------------------------------------------------------------------------

_STATION_FIELDS = (
    "id, operator_id, name, address, lat, lng, connector_type, power_kw, mode, "
    "ocpp_charge_point_id, tariff_uah_kwh, tariff_uah_start, qr_slug, status, created_at"
)


async def create_station(operator_id: int, name: str, tariff_uah_kwh: float,
                         address: str = None, lat: float = None, lng: float = None,
                         connector_type: str = None, power_kw: float = None,
                         mode: str = "manual", ocpp_charge_point_id: str = None,
                         tariff_uah_start: float = None, qr_slug: str = None):
    """Додає станцію оператору. Повертає (station_id, qr_slug)."""
    slug = qr_slug or generate_qr_slug()
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        station_id = await conn.fetchval("""
            INSERT INTO operator_stations (
                operator_id, name, address, lat, lng, connector_type, power_kw,
                mode, ocpp_charge_point_id, tariff_uah_kwh, tariff_uah_start, qr_slug
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING id
        """, operator_id, name, address, lat, lng, connector_type, power_kw,
             mode, ocpp_charge_point_id, tariff_uah_kwh, tariff_uah_start, slug)
        return station_id, slug


async def list_stations(operator_id: int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(f"""
            SELECT {_STATION_FIELDS} FROM operator_stations
            WHERE operator_id = $1
            ORDER BY created_at
        """, operator_id)


async def get_station(operator_id: int, station_id: int):
    """
    Станція В МЕЖАХ тенанта. Чужий station_id поверне None, а не рядок
    іншого оператора — саме це перевіряють тести ізоляції.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(f"""
            SELECT {_STATION_FIELDS} FROM operator_stations
            WHERE id = $2 AND operator_id = $1
        """, operator_id, station_id)


async def get_station_by_qr_slug(qr_slug: str):
    """
    ПУБЛІЧНИЙ доступ для QR-флоу водія (/s/{qr_slug}) — без operator_id,
    бо водій не автентифікований, а slug сам є секретом. Повертає в тому
    числі operator_id, щоб подальші виклики знову були тенант-скоупнуті.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(f"""
            SELECT {_STATION_FIELDS} FROM operator_stations WHERE qr_slug = $1
        """, qr_slug)


async def get_station_by_ocpp_charge_point_id(charge_point_id: str):
    """Службовий доступ для OCPP-сервера (Промпт 3): станція за websocket-ідентифікатором."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(f"""
            SELECT {_STATION_FIELDS} FROM operator_stations
            WHERE ocpp_charge_point_id = $1
        """, charge_point_id)


async def list_public_stations_near(lat: float, lng: float, radius_km: float = 30):
    """
    ПУБЛІЧНИЙ пошук станцій для водія (Промпт 4c) — третій свідомий виняток
    з правила ізоляції, див. докстрінг модуля. Повертає лише активні станції
    активних операторів із заданими координатами, відсортовані за
    відстанню зростанням; кожен рядок додатково несе 'distance_km'.

    SQL відсіює статуси й відсутні координати (дешево — індекс на
    operator_id уже є, датасет малий). Сама відстань — Python, через
    haversine_km() (app/services/geo.py): PostGIS для MVP надлишковий, а
    чиста функція значно легше тестується, ніж формула, зашита в SQL.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT {', '.join(f's.{f.strip()}' for f in _STATION_FIELDS.split(','))}
            FROM operator_stations s
            JOIN operators o ON o.id = s.operator_id
            WHERE s.status = 'active' AND o.status = 'active'
              AND s.lat IS NOT NULL AND s.lng IS NOT NULL
        """)

    nearby = []
    for row in rows:
        distance_km = haversine_km(lat, lng, float(row["lat"]), float(row["lng"]))
        if distance_km <= radius_km:
            nearby.append((distance_km, row))
    nearby.sort(key=lambda pair: pair[0])
    return [{**dict(row), "distance_km": distance_km} for distance_km, row in nearby]


async def update_station_tariff(operator_id: int, station_id: int,
                                tariff_uah_kwh: float, tariff_uah_start: float = None):
    """Повертає True, лише якщо станція справді належить цьому оператору."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE operator_stations
            SET tariff_uah_kwh = $3, tariff_uah_start = $4
            WHERE id = $2 AND operator_id = $1
        """, operator_id, station_id, tariff_uah_kwh, tariff_uah_start)
        return result.endswith("1")


async def set_station_status(operator_id: int, station_id: int, status: str):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE operator_stations SET status = $3
            WHERE id = $2 AND operator_id = $1
        """, operator_id, station_id, status)
        return result.endswith("1")


# ---------------------------------------------------------------------------
# Сесії зарядки
# ---------------------------------------------------------------------------

_SESSION_FIELDS = (
    "id, operator_id, station_id, started_at, ended_at, kwh, amount_uah, "
    "payment_id, status, driver_contact, created_at"
)


async def create_session(operator_id: int, station_id: int, amount_uah: float = None,
                         payment_id: int = None, driver_contact: str = None):
    """
    Створює сесію. operator_id береться НЕ з аргументу напряму, а з самої
    станції через INSERT ... SELECT з фільтром `operator_id = $1`: якщо
    станція належить іншому оператору, SELECT нічого не поверне і сесія
    просто не створиться (None). Тобто крос-тенантну сесію неможливо
    записати навіть навмисно — ні через баг у виклику, ні через підміну id.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            INSERT INTO operator_sessions (
                operator_id, station_id, amount_uah, payment_id, driver_contact, status
            )
            SELECT s.operator_id, s.id, $3, $4, $5, 'pending'
            FROM operator_stations s
            WHERE s.id = $2 AND s.operator_id = $1
            RETURNING id
        """, operator_id, station_id, amount_uah, payment_id, driver_contact)


async def get_session(operator_id: int, session_id: int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(f"""
            SELECT {_SESSION_FIELDS} FROM operator_sessions
            WHERE id = $2 AND operator_id = $1
        """, operator_id, session_id)


async def list_sessions(operator_id: int, limit: int = 50, station_id: int = None):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        if station_id is not None:
            return await conn.fetch(f"""
                SELECT {_SESSION_FIELDS} FROM operator_sessions
                WHERE operator_id = $1 AND station_id = $2
                ORDER BY started_at DESC
                LIMIT $3
            """, operator_id, station_id, limit)
        return await conn.fetch(f"""
            SELECT {_SESSION_FIELDS} FROM operator_sessions
            WHERE operator_id = $1
            ORDER BY started_at DESC
            LIMIT $2
        """, operator_id, limit)


async def set_session_status(operator_id: int, session_id: int, status: str,
                             payment_id: int = None):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE operator_sessions
            SET status = $3,
                payment_id = COALESCE($4, payment_id)
            WHERE id = $2 AND operator_id = $1
        """, operator_id, session_id, status, payment_id)
        return result.endswith("1")


async def complete_session(operator_id: int, session_id: int, kwh: float,
                           amount_uah: float = None):
    """Фіналізує сесію: фактичні кВт·год, сума і час завершення."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE operator_sessions
            SET kwh = $3,
                amount_uah = COALESCE($4, amount_uah),
                ended_at = CURRENT_TIMESTAMP,
                status = 'completed'
            WHERE id = $2 AND operator_id = $1
        """, operator_id, session_id, kwh, amount_uah)
        return result.endswith("1")


# ---------------------------------------------------------------------------
# Платежі водіїв (еквайринг оператора)
# ---------------------------------------------------------------------------

_PAYMENT_FIELDS = (
    "id, operator_id, invoice_id, amount_uah, status, created_at, updated_at"
)


async def create_operator_payment(operator_id: int, invoice_id: str, amount_uah,
                                  status: str = "pending"):
    """
    Реєструє платіж водія. Повертає id, або id вже наявного рядка, якщо
    цей invoice_id для цього оператора вже записаний (повторна спроба
    створення інвойсу не має плодити дублікати).
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        payment_id = await conn.fetchval("""
            INSERT INTO operator_payments (operator_id, invoice_id, amount_uah, status)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (operator_id, invoice_id) DO NOTHING
            RETURNING id
        """, operator_id, invoice_id, amount_uah, status)
        if payment_id is not None:
            return payment_id
        return await conn.fetchval("""
            SELECT id FROM operator_payments
            WHERE operator_id = $1 AND invoice_id = $2
        """, operator_id, invoice_id)


async def get_operator_payment_by_invoice(operator_id: int, invoice_id: str):
    """
    Платіж за invoice_id У МЕЖАХ оператора.

    Саме ця функція не дає webhook'ові оператора А підтвердити інвойс
    оператора Б: operator_id береться з URL webhook, і чужий invoice_id
    просто не знаходиться.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(f"""
            SELECT {_PAYMENT_FIELDS} FROM operator_payments
            WHERE operator_id = $1 AND invoice_id = $2
        """, operator_id, invoice_id)


async def get_operator_payment(operator_id: int, payment_id: int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(f"""
            SELECT {_PAYMENT_FIELDS} FROM operator_payments
            WHERE operator_id = $1 AND id = $2
        """, operator_id, payment_id)


async def set_operator_payment_status(operator_id: int, payment_id: int, status: str,
                                      payload: str = None, conn=None):
    """
    Оновлює статус платежу. payload — сирий JSON відповіді банку (рядок),
    зберігаємо для розборів і звірки.

    Умова `status <> $3` робить оновлення ідемпотентним: повторний webhook
    з тим самим статусом не чіпає рядок і повертає False, тож викликач
    бачить «нічого нового не сталось» і не проводить нарахування вдруге.
    """
    query = """
        UPDATE operator_payments
        SET status = $3,
            payload = COALESCE($4::jsonb, payload),
            updated_at = CURRENT_TIMESTAMP
        WHERE operator_id = $1 AND id = $2 AND status <> $3
    """
    if conn is not None:
        result = await conn.execute(query, operator_id, payment_id, status, payload)
    else:
        pool = await get_db_pool()
        async with pool.acquire() as new_conn:
            result = await new_conn.execute(query, operator_id, payment_id, status, payload)
    return result.endswith("1")


async def attach_payment_to_session(operator_id: int, session_id: int, payment_id: int):
    """Привʼязує створений інвойс до сесії (обидва — цього ж оператора)."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE operator_sessions SET payment_id = $3
            WHERE operator_id = $1 AND id = $2
        """, operator_id, session_id, payment_id)
        return result.endswith("1")


async def get_session_by_payment(operator_id: int, payment_id: int, conn=None):
    """Сесія, до якої привʼязаний платіж (uq_sessions_payment гарантує одну)."""
    query = f"""
        SELECT {_SESSION_FIELDS} FROM operator_sessions
        WHERE operator_id = $1 AND payment_id = $2
    """
    if conn is not None:
        return await conn.fetchrow(query, operator_id, payment_id)
    pool = await get_db_pool()
    async with pool.acquire() as new_conn:
        return await new_conn.fetchrow(query, operator_id, payment_id)


# ---------------------------------------------------------------------------
# Звірка (reconcile_operators.py, Промпт 5) — виключно SELECT-и, що шукають
# розбіжності; самі виправлення йдуть уже наявними функціями вище
# (apply_bank_status/complete_paid_session в app/api/operator_webhook.py).
# ---------------------------------------------------------------------------

async def list_pending_payments_older_than(operator_id: int, older_than):
    """
    Pending-платежі оператора, створені раніше за `older_than` (звірка
    рахує від нього INVOICE_TTL_SECONDS назад від "зараз"). Банк для таких
    інвойсів уже мав або підтвердити оплату, або протермінувати їх — якщо
    наш webhook про це не почув, це і є розбіжність, яку добирає звірка.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(f"""
            SELECT {_PAYMENT_FIELDS} FROM operator_payments
            WHERE operator_id = $1 AND status = 'pending' AND created_at < $2
            ORDER BY created_at
        """, operator_id, older_than)


async def list_success_payments_without_income(operator_id: int):
    """
    Успішні платежі оператора, привʼязані до сесії, але без проведеного
    'session_income' у журналі — слід перерваного ланцюга «платіж success,
    процес впав до запису доходу» (задокументовано раніше в
    app/api/operator_webhook.py). Повертає поля платежу плюс session_id для
    complete_paid_session().
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT p.id, p.operator_id, p.invoice_id, p.amount_uah, p.status,
                   p.created_at, p.updated_at, s.id AS session_id
            FROM operator_payments p
            JOIN operator_sessions s
                ON s.operator_id = p.operator_id AND s.payment_id = p.id
            LEFT JOIN operator_payout_ledger l
                ON l.operator_id = p.operator_id AND l.session_id = s.id
                   AND l.type = 'session_income'
            WHERE p.operator_id = $1 AND p.status = 'success' AND l.id IS NULL
            ORDER BY p.created_at
        """, operator_id)


async def list_success_payments_without_session(operator_id: int):
    """
    Успішні платежі оператора, до яких НЕ привʼязано жодної сесії —
    доходу нараховувати нікуди (не з чим повʼязати), тому лише алерт на
    ручний розбір, а не автоматичне виправлення.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(f"""
            SELECT {_PAYMENT_FIELDS} FROM operator_payments p
            WHERE p.operator_id = $1 AND p.status = 'success'
              AND NOT EXISTS (
                  SELECT 1 FROM operator_sessions s
                  WHERE s.operator_id = p.operator_id AND s.payment_id = p.id
              )
            ORDER BY p.created_at
        """, operator_id)


async def list_stale_pending_sessions_without_payment(operator_id: int, older_than):
    """
    Pending-сесії оператора без payment_id, старші за `older_than` — слід
    вікна «інвойс у банку створено, наш рядок operator_payments не встиг
    записатись» (app/api/driver_qr.py). Автоматично тут нічого не
    виправити (invoice_id нам невідомий), тому назва станції — щоб алерт
    можна було одразу звірити з випискою банку вручну.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT sess.id, sess.operator_id, sess.station_id, sess.started_at,
                   sess.amount_uah, sess.status, sess.created_at,
                   st.name AS station_name
            FROM operator_sessions sess
            JOIN operator_stations st
                ON st.id = sess.station_id AND st.operator_id = sess.operator_id
            WHERE sess.operator_id = $1 AND sess.status = 'pending'
              AND sess.payment_id IS NULL AND sess.created_at < $2
            ORDER BY sess.created_at
        """, operator_id, older_than)


# ---------------------------------------------------------------------------
# Журнал розрахунків з оператором
# ---------------------------------------------------------------------------

async def add_ledger_entry(operator_id: int, entry_type: str, amount_uah: float,
                           session_id: int = None, description: str = None, conn=None):
    """
    Єдина точка запису в operator_payout_ledger.

    Знак amount_uah — відповідальність викликача, але семантика фіксована:
    session_income — плюс, platform_commission / subscription_fee / payout —
    мінус, adjustment — будь-який. Журнал незмінний: виправлення робиться
    новим рядком типу 'adjustment', а не UPDATE/DELETE.

    Якщо передано `conn` — пишемо в транзакції викликача (та сама механіка,
    що й в update_user_balance()).

    ON CONFLICT DO NOTHING разом із частковим унікальним індексом
    uq_ledger_session_income робить повторне проведення сесії безпечним:
    другий запис доходу/комісії по тій самій сесії не створюється, і
    функція повертає None замість id. Викликач мусить цей None обробити —
    див. record_session_income(). Рядків без session_id ('payout',
    'subscription_fee', 'adjustment') обмеження не стосується взагалі,
    тому вони, як і раніше, можуть повторюватись.
    """
    query = """
        INSERT INTO operator_payout_ledger (operator_id, session_id, type, amount_uah, description)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT DO NOTHING
        RETURNING id
    """
    if conn is not None:
        return await conn.fetchval(query, operator_id, session_id, entry_type,
                                   amount_uah, description)

    pool = await get_db_pool()
    async with pool.acquire() as new_conn:
        return await new_conn.fetchval(query, operator_id, session_id, entry_type,
                                       amount_uah, description)


async def get_session_income_entries(operator_id: int, session_id: int, conn=None):
    """
    Уже проведені дохід і комісія по сесії: {'session_income': id,
    'platform_commission': id}. Порожній dict, якщо сесія ще не проводилась.
    """
    query = """
        SELECT type, id FROM operator_payout_ledger
        WHERE operator_id = $1 AND session_id = $2
          AND type IN ('session_income', 'platform_commission')
    """
    if conn is not None:
        rows = await conn.fetch(query, operator_id, session_id)
    else:
        pool = await get_db_pool()
        async with pool.acquire() as new_conn:
            rows = await new_conn.fetch(query, operator_id, session_id)
    return {row["type"]: row["id"] for row in rows}


async def record_session_income(operator_id: int, session_id: int, amount_uah: float,
                                commission_pct: float):
    """
    Дохід оператора з оплаченої сесії та наша комісія — двома рядками журналу
    в ОДНІЙ транзакції, щоб дохід ніколи не існував без відповідної комісії.
    Повертає (income_id, commission_id).

    ІДЕМПОТЕНТНО: повторний виклик з тим самим session_id (повторний webhook
    Monobank, ретрай після таймауту, подвійне натискання оператором) НЕ
    нараховує дохід удруге — частковий унікальний індекс
    uq_ledger_session_income відхиляє вставку, і функція повертає id уже
    існуючих рядків. Це важливо саме тут, бо журнал незмінний: зайве
    нарахування довелося б потім гасити ручним рядком 'adjustment'.

    Обидві вставки або проходять, або конфліктують разом — індекс покриває
    обидва типи, а транзакція не дає зупинитись посередині.

    Комісія рахується в Decimal, а не у float з round(): round() у Python
    використовує банківське округлення (round(5.625, 2) -> 5.62, до
    парного), тому на копійках дає систематичний зсув не на нашу користь —
    непомітний на одній сесії й цілком помітний на тисячах. Decimal з
    явним ROUND_HALF_UP округлює так, як очікує бухгалтерія, і не тягне
    двійкову похибку float у гроші. Через це в журнал іде Decimal —
    NUMERIC(12,2) приймає його напряму, без проміжного float.
    """
    commission = (
        Decimal(str(amount_uah)) * Decimal(str(commission_pct)) / 100
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            income_id = await add_ledger_entry(
                operator_id, "session_income", amount_uah, session_id=session_id,
                description=f"Оплата сесії #{session_id}", conn=conn,
            )
            commission_id = await add_ledger_entry(
                operator_id, "platform_commission", -commission, session_id=session_id,
                description=f"Комісія платформи {commission_pct}% з сесії #{session_id}",
                conn=conn,
            )

            if income_id is not None and commission_id is not None:
                return income_id, commission_id

            # Конфлікт унікального індексу — сесію вже проводили раніше.
            existing = await get_session_income_entries(operator_id, session_id, conn=conn)
            logger.warning(
                "duplicate income ignored: сесія #%s оператора %s уже проведена "
                "(дохід=%s, комісія=%s) — повторне нарахування пропущено",
                session_id, operator_id,
                existing.get("session_income"), existing.get("platform_commission"),
            )
            return existing.get("session_income"), existing.get("platform_commission")


async def get_operator_balance(operator_id: int):
    """
    Баланс = SUM журналу. Кешованої колонки навмисно немає — див. коментар
    у міграції 0010: у цьому проєкті кеш балансу вже тричі розходився з журналом.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval("""
            SELECT COALESCE(SUM(amount_uah), 0) FROM operator_payout_ledger
            WHERE operator_id = $1
        """, operator_id)
        return float(value or 0)


async def list_ledger(operator_id: int, limit: int = 50):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT id, operator_id, session_id, type, amount_uah, description, created_at
            FROM operator_payout_ledger
            WHERE operator_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        """, operator_id, limit)


async def list_ledger_since(operator_id: int, since, limit: int = 1000):
    """
    Записи журналу з моменту `since` у ХРОНОЛОГІЧНОМУ порядку — основа для
    CSV-вивантаження виручки (Промпт 4). На відміну від list_ledger()
    (найновіші перші, для перегляду в чаті) експорт читається зверху вниз.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT id, operator_id, session_id, type, amount_uah, description, created_at
            FROM operator_payout_ledger
            WHERE operator_id = $1 AND created_at >= $2
            ORDER BY created_at
            LIMIT $3
        """, operator_id, since, limit)


async def get_ledger_summary(operator_id: int, since):
    """
    SUM(amount_uah) за типом з моменту `since` -> {'session_income': Decimal, ...}.
    Тип, за яким за період не було жодного запису, у результаті відсутній
    (не 0) — нуль за замовчуванням рахує викликач, тут COALESCE не потрібен.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT type, SUM(amount_uah) AS total
            FROM operator_payout_ledger
            WHERE operator_id = $1 AND created_at >= $2
            GROUP BY type
        """, operator_id, since)
        return {row["type"]: row["total"] for row in rows}
