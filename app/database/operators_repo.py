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

Схема продубльована в migrations/versions/0010_white_label_tenants.py —
при зміні оновлювати ОБИДВА місця (конвенція проєкту, див. PROJECT_CONTEXT.md).
"""
import logging
import secrets

from app.database.connection import get_db_pool

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
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (id)
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
            payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
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
    """
    query = """
        INSERT INTO operator_payout_ledger (operator_id, session_id, type, amount_uah, description)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
    """
    if conn is not None:
        return await conn.fetchval(query, operator_id, session_id, entry_type,
                                   amount_uah, description)

    pool = await get_db_pool()
    async with pool.acquire() as new_conn:
        return await new_conn.fetchval(query, operator_id, session_id, entry_type,
                                       amount_uah, description)


async def record_session_income(operator_id: int, session_id: int, amount_uah: float,
                                commission_pct: float):
    """
    Дохід оператора з оплаченої сесії та наша комісія — двома рядками журналу
    в ОДНІЙ транзакції, щоб дохід ніколи не існував без відповідної комісії.
    Повертає (income_id, commission_id).
    """
    commission = round(amount_uah * commission_pct / 100, 2)
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
            return income_id, commission_id


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
