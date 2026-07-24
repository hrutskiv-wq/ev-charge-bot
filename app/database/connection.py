import os
import logging
import asyncio
import asyncpg

# Глобальний пул підключень та прапорець ініціалізації
db_pool = None
_initializing = False

PRICE_PER_KWH = 15.0  # Вартість 1 кВт·год у гривнях

def uah_to_kwh(amount_uah: float) -> float:
    return amount_uah / PRICE_PER_KWH

def kwh_to_uah(amount_kwh: float) -> float:
    return amount_kwh * PRICE_PER_KWH

async def get_db_pool():
    """Безпечне отримання або лінива ініціалізація пулу підключень (запобігає race condition та пропущеним startup-подіям)"""
    global db_pool, _initializing
    if db_pool is not None:
        return db_pool
    
    if not _initializing:
        _initializing = True
        try:
            logging.info("🔌 Пул підключень не знайдено. Автоматично запускаємо ініціалізацію бази...")
            await init_postgres()
        finally:
            _initializing = False
    else:
        # Якщо ініціалізація вже триває іншим таском, просто чекаємо її завершення
        for _ in range(50):
            await asyncio.sleep(0.1)
            if db_pool is not None:
                return db_pool
                
    if db_pool is None:
        raise RuntimeError("❌ Не вдалося автоматично ініціалізувати пул підключень до PostgreSQL!")
    return db_pool

async def init_postgres(retries: int = 5):
    """Ініціалізація пулу підключень із повторними спробами"""
    global db_pool
    db_url = os.getenv("DB_URL")
    if db_url and db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        
    logging.info("⚙️ Спроба підключення до PostgreSQL...")
    for attempt in range(1, retries + 1):
        try:
            db_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=10)
            logging.info("✅ Пул підключень до PostgreSQL успішно створено!")
            await create_tables(db_pool)
            return
        except Exception as e:
            logging.warning(f"⚠️ База даних ще не готова (спроба {attempt}/{retries}): {e}")
            if attempt < retries:
                await asyncio.sleep(3)
            else:
                logging.error("💥 Не вдалося підключитися до PostgreSQL після всіх спроб!")
                raise e

async def create_tables(pool=None):
    """Створення базових таблиць + Ledger-архітектури білінгу"""
    if pool is None:
        pool = db_pool
    if not pool:
        return
        
    async with pool.acquire() as conn:
        # 1. Створення типів ENUM
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status') THEN
                    CREATE TYPE payment_status AS ENUM ('pending', 'success', 'failed', 'refunded');
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_provider') THEN
                    CREATE TYPE payment_provider AS ENUM ('monobank', 'telegram');
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'transaction_type') THEN
                    CREATE TYPE transaction_type AS ENUM ('deposit', 'withdrawal', 'bonus', 'correction', 'refund', 'hold', 'release');
                END IF;
            END $$;
        """)

        # 2. Таблиця користувачів
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance NUMERIC(10, 2) DEFAULT 0.00,
                discount NUMERIC(3, 2) DEFAULT 1.00,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 3. Таблиця станцій
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stations (
                id VARCHAR(50) PRIMARY KEY,
                name VARCHAR(255),
                address TEXT,
                connectors TEXT,
                lat NUMERIC(9,6),
                lon NUMERIC(9,6),
                operator VARCHAR(255),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 4. Таблиця платежів (для інвойсів Monobank)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                invoice_id VARCHAR(100) UNIQUE NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                provider payment_provider NOT NULL,
                status payment_status DEFAULT 'pending',
                payload JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 5. Ledger-журнал балансу електроенергії (кВт·год)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kw_transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                type transaction_type NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
                session_id VARCHAR(100),
                description TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # На старих БД, розгорнутих до додавання цієї колонки, ADD COLUMN
        # IF NOT EXISTS підтягне схему без ручного запуску Alembic-міграції.
        await conn.execute("ALTER TABLE kw_transactions ADD COLUMN IF NOT EXISTS session_id VARCHAR(100);")

        # 6. CDR-и, отримані від CPO по OCPI. Раніше ця таблиця створювалась
        # лише в Alembic-міграції, тому на щойно розгорнутій базі (без
        # `alembic upgrade head`) прийом CDR падав з UndefinedTableError.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ocpi_cdrs (
                id SERIAL PRIMARY KEY,
                cdr_id VARCHAR(100) UNIQUE NOT NULL,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                session_id VARCHAR(100) NOT NULL,
                total_energy NUMERIC(10, 4) NOT NULL,
                total_cost NUMERIC(10, 2) NOT NULL,
                raw_payload JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Високопродуктивні індекси
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_kw_transactions_user ON kw_transactions(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ocpi_cdrs_user_id ON ocpi_cdrs(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ocpi_cdrs_session_id ON ocpi_cdrs(session_id);")
        logging.info("📊 Усі таблиці PostgreSQL та індекси верифіковано.")

async def close_postgres():
    global db_pool
    if db_pool:
        await db_pool.close()
        logging.info("💤 Пул підключень до PostgreSQL закрито.")

async def get_user_data(user_id: int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, 0.00) ON CONFLICT DO NOTHING", user_id)
        row = await conn.fetchrow("SELECT balance, discount FROM users WHERE user_id = $1", user_id)
        return float(row['balance']), float(row['discount'])

async def update_user_balance(
    user_id: int,
    amount_kwh: float,
    t_type: str = "deposit",
    conn=None,
    session_id: str = None,
    payment_id: int = None,
    description: str = None,
):
    """
    Єдина точка запису балансу (кВт·год). Атомарно оновлює кешоване
    users.balance І пише запис у журнал kw_transactions в одній транзакції,
    тому вони ніколи не розходяться між собою.

    ВАЖЛИВО про знак amount у kw_transactions: депозит пишеться додатним
    числом, списання — від'ємним. Це зроблено навмисно, бо в кількох
    місцях системи (app/services/ocpi/commands_service.py,
    app/handlers/ocpi_stations.py) баланс рахується як
    SUM(kw_transactions.amount) — якщо списання зберігати додатним числом,
    ця сума лише зростає і ніколи не відображає реальні витрати.

    Якщо передано `conn` — операція виконується в межах транзакції
    викликача (наприклад, разом із записом CDR в app/api/ocpi.py), інакше
    функція сама відкриває з'єднання та транзакцію.

    t_type="refund" — компенсація користувачу (наприклад, CDR прийшов і
    кВт·год списались, а фізична сесія зарядки не відбулась). Це КРЕДИТ
    (додає до балансу, як і депозит), але в журналі kw_transactions
    записується окремим типом 'refund' (не 'deposit'), щоб відрізняти
    компенсації від звичайних поповнень у звітності й реконсиляції.
    Раніше в enum transaction_type взагалі не було значення 'refund' у
    реальній (Alembic) схемі — див. migrations/versions/0008_add_refund_transaction_type.py.

    t_type="hold" / t_type="release" (Промпт 3c-i, kWh-резервація на OCPP-
    сесію): "hold" — ДЕБЕТ з явним запобіжником `balance >= $1` у SQL —
    на відміну від УСІХ інших гілок списання (`ocpi_session` тощо), тут
    баланс НЕ МАЄ права піти в мінус, інакше та сама сума могла б піти на
    дві паралельні сесії. Повертає False (і НІЧОГО не пише — ні в
    users.balance, ні в kw_transactions), якщо балансу не вистачає —
    викликач (app/database/operators_repo.py::create_charging_reservation)
    відкочує всю резервацію. "release" — КРЕДИТ (як deposit/refund), для
    звільнення невикористаної частини резерву; окремий ledger-тип, щоб не
    плутати з депозитом/компенсацією у звітності.

    Повертає True/False: чи справді відбулась зміна балансу (для "hold" —
    False саме тоді, коли балансу не вистачило; для решти t_type завжди
    True — там немає умовного WHERE).
    """

    async def _apply(active_conn) -> bool:
        await active_conn.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
        )
        if t_type in ["deposit", "monobank_jar", "refund"]:
            await active_conn.execute(
                "UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount_kwh, user_id
            )
            ledger_type = "refund" if t_type == "refund" else "deposit"
            default_desc = (
                "Повернення коштів (компенсація)" if t_type == "refund"
                else "Поповнення балансу (Ваучер / Адмін)"
            )
            desc = description or default_desc
            await active_conn.execute(
                """
                INSERT INTO kw_transactions (user_id, type, amount, payment_id, session_id, description)
                VALUES ($1, $2::transaction_type, $3, $4, $5, $6)
                """,
                user_id, ledger_type, amount_kwh, payment_id, session_id, desc,
            )
            return True

        if t_type == "release":
            await active_conn.execute(
                "UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount_kwh, user_id
            )
            desc = description or "Звільнення невикористаного резерву"
            await active_conn.execute(
                """
                INSERT INTO kw_transactions (user_id, type, amount, payment_id, session_id, description)
                VALUES ($1, $2::transaction_type, $3, $4, $5, $6)
                """,
                user_id, "release", amount_kwh, payment_id, session_id, desc,
            )
            return True

        if t_type == "hold":
            result = await active_conn.execute(
                "UPDATE users SET balance = balance - $1 WHERE user_id = $2 AND balance >= $1",
                amount_kwh, user_id,
            )
            if not result.endswith("1"):
                return False
            desc = description or "Резерв кВт·год на сесію зарядки"
            await active_conn.execute(
                """
                INSERT INTO kw_transactions (user_id, type, amount, payment_id, session_id, description)
                VALUES ($1, $2::transaction_type, $3, $4, $5, $6)
                """,
                user_id, "hold", -amount_kwh, payment_id, session_id, desc,
            )
            return True

        # Загальне списання (напр. t_type="ocpi_session") — БЕЗ ЗМІН.
        await active_conn.execute(
            "UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount_kwh, user_id
        )
        desc = description or f"Списання за сесію зарядки ({t_type})"
        await active_conn.execute(
            """
            INSERT INTO kw_transactions (user_id, type, amount, payment_id, session_id, description)
            VALUES ($1, 'withdrawal', $2, $3, $4, $5)
            """,
            user_id, -amount_kwh, payment_id, session_id, desc,
        )
        return True

    if conn is not None:
        return await _apply(conn)

    pool = await get_db_pool()
    async with pool.acquire() as new_conn:
        async with new_conn.transaction():
            return await _apply(new_conn)

async def get_user_transactions(user_id: int, limit: int = 10):
    pool = await get_db_pool()
    try:
        async with pool.acquire() as conn:
            return await conn.fetch("""
                SELECT type, amount, description, created_at 
                FROM kw_transactions 
                WHERE user_id = $1 
                ORDER BY created_at DESC 
                LIMIT $2
            """, user_id, limit)
    except Exception as e:
        logging.error(f"❌ Помилка отримання історії для {user_id}: {e}")
        return []

async def save_ocpi_tariff(tariff_id: str, price_per_kwh: float):
    pool = await get_db_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tariffs (
                    id VARCHAR(100) PRIMARY KEY,
                    price_per_kwh NUMERIC(10, 2) NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await conn.execute("""
                INSERT INTO tariffs (id, price_per_kwh, updated_at) 
                VALUES ($1, $2, CURRENT_TIMESTAMP) 
                ON CONFLICT (id) DO UPDATE SET price_per_kwh = $2, updated_at = CURRENT_TIMESTAMP;
            """, tariff_id, price_per_kwh)
    except Exception as e:
        logging.error(f"❌ Помилка збереження тарифу {tariff_id}: {e}")

async def set_user_discount(user_id: int, discount_value: float):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        await conn.execute("UPDATE users SET discount = $1 WHERE user_id = $2", discount_value, user_id)

async def create_pending_payment(user_id: int, invoice_id: str, amount: float, provider: str = "monobank"):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments (user_id, invoice_id, amount, provider, status)
            VALUES ($1, $2, $3, $4, 'pending')
        """, user_id, invoice_id, amount, provider)
        logging.info(f"📝 Створено рахунок {invoice_id} на суму {amount} грн для {user_id}")

async def save_station_to_local_db(station_id: str, name: str, address: str, connectors: str, lat: float, lon: float, operator: str):
    pool = await get_db_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO stations (id, name, address, connectors, lat, lon, operator, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE 
                SET name = $2, address = $3, connectors = $4, lat = $5, lon = $6, operator = $7, updated_at = CURRENT_TIMESTAMP;
            """, station_id, name, address, connectors, lat, lon, operator)
    except Exception as e:
        logging.error(f"⚠️ Не вдалося зберегти станцію {station_id}: {e}")

async def get_station_by_id(station_id: str):
    pool = await get_db_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name, address, connectors FROM stations WHERE id = $1", station_id)
            if row:
                return row['name'], row['address'], row['connectors']
    except Exception as e:
        logging.error(f"❌ Помилка отримання станції {station_id}: {e}")
    return ("Тестовий Комплекс eVolt", "вулиця Зубра, 17", "Type 2, CCS 2")
