import os
import logging
import asyncio
import asyncpg

# Глобальний пул підключень
db_pool = None

PRICE_PER_KWH = 15.0  # Вартість 1 кВт·год у гривнях

def uah_to_kwh(amount_uah: float) -> float:
    return amount_uah / PRICE_PER_KWH

def kwh_to_uah(amount_kwh: float) -> float:
    return amount_kwh * PRICE_PER_KWH

async def init_postgres():
    """Ініціалізація пулу підключень із логікою повторних спроб"""
    global db_pool
    db_url = os.getenv("DB_URL")
    if db_url and db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        
    logging.info("⚙️ Спроба підключення до PostgreSQL...")
    retries = 5
    for attempt in range(1, retries + 1):
        try:
            db_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=10)
            logging.info("✅ Пул підключень до PostgreSQL успішно створено!")
            await create_tables()
            return
        except Exception as e:
            logging.warning(f"⚠️ База даних ще не готова (спроба {attempt}/{retries}): {e}")
            if attempt < retries:
                await asyncio.sleep(3)
            else:
                logging.error("💥 Не вдалося підключитися до PostgreSQL після всіх спроб!")
                raise e

async def create_tables():
    """Створення базових таблиць + нової Ledger-архітектури білінгу"""
    global db_pool
    if not db_pool:
        return
        
    async with db_pool.acquire() as conn:
        await conn.execute("""
            DO $$ 
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status') THEN
                    CREATE TYPE payment_status AS ENUM ('pending', 'success', 'failed', 'refunded');
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_provider') THEN
                    CREATE TYPE payment_provider AS ENUM ('liqpay', 'monobank', 'telegram');
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'transaction_type') THEN
                    CREATE TYPE transaction_type AS ENUM ('deposit', 'withdrawal', 'bonus', 'correction', 'refund');
                END IF;
            END $$;
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance NUMERIC(10, 2) DEFAULT 0.00,
                discount NUMERIC(3, 2) DEFAULT 1.00,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # НОВА ТАБЛИЦЯ ДЛЯ ТАРИФІВ OCPI
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tariffs (
                tariff_id VARCHAR(50) PRIMARY KEY,
                currency VARCHAR(10) DEFAULT 'UAH',
                price_per_kwh NUMERIC(10, 2) NOT NULL,
                last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stations (
                id VARCHAR(50) PRIMARY KEY,
                name VARCHAR(255),
                address TEXT,
                connectors TEXT,
                lat NUMERIC(9,6),
                lon NUMERIC(9,6),
                operator VARCHAR(255),
                tariff_id VARCHAR(50) REFERENCES tariffs(tariff_id) ON DELETE SET NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                amount NUMERIC(10, 2),
                type VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                invoice_id VARCHAR(100) UNIQUE NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                provider payment_provider NOT NULL,
                status payment_status DEFAULT 'pending',
                payload JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kw_transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                type transaction_type NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
                session_id INTEGER,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_kw_transactions_user ON kw_transactions(user_id);")

        logging.info("📊 Усі таблиці PostgreSQL (включені тарифи) успішно перевірені/створені!")

# ФУНКЦІЯ ДЛЯ ПОРЯДКУВАННЯ ТАРИФІВ В БАЗІ
async def save_ocpi_tariff(tariff_id: str, price: float, currency: str = "UAH"):
    global db_pool
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO tariffs (tariff_id, price_per_kwh, currency, last_updated)
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (tariff_id) DO UPDATE
            SET price_per_kwh = $2, currency = $3, last_updated = CURRENT_TIMESTAMP;
        """, tariff_id, price, currency)
        logging.info(f"💾 Тариф {tariff_id} збережено в базу. Собівартість: {price} {currency}/кВт·год")

async def close_postgres():
    global db_pool
    if db_pool:
        await db_pool.close()
        logging.info("💤 Пул підключень до PostgreSQL закрито.")

async def get_user_data(user_id: int):
    global db_pool
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, 0.00) ON CONFLICT DO NOTHING", user_id)
        row = await conn.fetchrow("SELECT balance, discount FROM users WHERE user_id = $1", user_id)
        return float(row['balance']), float(row['discount'])

async def update_user_balance(user_id: int, amount_kwh: float, t_type: str = "deposit"):
    global db_pool
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
            if t_type == "deposit":
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount_kwh, user_id)
                await conn.execute("""
                    INSERT INTO kw_transactions (user_id, type, amount, description) 
                    VALUES ($1, 'deposit', $2, $3)
                """, user_id, amount_kwh, "Поповнення балансу (Ваучер / Адмін)")
            else:
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount_kwh, user_id)
                await conn.execute("""
                    INSERT INTO kw_transactions (user_id, type, amount, description) 
                    VALUES ($1, 'withdrawal', $2, $3)
                """, user_id, amount_kwh, f"Списання за сесію зарядки ({t_type})")

async def set_user_discount(user_id: int, discount_value: float):
    global db_pool
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        await conn.execute("UPDATE users SET discount = $1 WHERE user_id = $2", discount_value, user_id)

async def create_pending_payment(user_id: int, invoice_id: str, amount: float, provider: str = "monobank"):
    global db_pool
    if not db_pool:
        return
    clean_provider = "monobank" if "mono" in provider.lower() else "telegram" if "telegram" in provider.lower() else "liqpay"
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments (user_id, invoice_id, amount, provider, status)
            VALUES ($1, $2, $3, $4, 'pending')
        """, user_id, invoice_id, amount, clean_provider)

async def save_station_to_local_db(station_id: str, name: str, address: str, connectors: str, lat: float, lon: float, operator: str):
    global db_pool
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO stations (id, name, address, connectors, lat, lon, operator, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE 
                SET name = $2, address = $3, connectors = $4, lat = $5, lon = $6, operator = $7, updated_at = CURRENT_TIMESTAMP;
            """, station_id, name, address, connectors, lat, lon, operator)
    except Exception as e:
        logging.error(f"⚠️ Не вдалося зберегти станцію {station_id}: {e}")

async def get_station_by_id(station_id: str):
    global db_pool
    if not db_pool:
        return ("Тестовий Комплекс eVolt", "вулиця Зубра, 17", "Type 2, CCS 2")
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name, address, connectors FROM stations WHERE id = $1", station_id)
            if row:
                return row['name'], row['address'], row['connectors']
    except Exception as e:
        logging.error(f"❌ Помилка отримання станції {station_id}: {e}")
    return ("Тестовий Комплекс eVolt", "вулиця Зубра, 17", "Type 2, CCS 2")

async def get_user_transactions(user_id: int, limit: int = 5):
    """Отримання останніх транзакцій користувача з Ledger-журналу"""
    global db_pool
    if not db_pool:
        return []
    try:
        async with db_pool.acquire() as conn:
            return await conn.fetch("""
                SELECT type, amount, description, created_at 
                FROM kw_transactions 
                WHERE user_id = $1 
                ORDER BY created_at DESC 
                LIMIT $2
            """, user_id, limit)
    except Exception as e:
        logging.error(f"❌ Помилка отримання історії транзакцій для {user_id}: {e}")
        return []
