import os
import logging
import asyncpg
from typing import Tuple, Optional

# Глобальний пул підключень
db_pool: Optional[asyncpg.Pool] = None

PRICE_PER_KWH = 15.0  # Вартість 1 кВт·год у гривнях

def uah_to_kwh(amount_uah: float) -> float:
    return amount_uah / PRICE_PER_KWH

def kwh_to_uah(amount_kwh: float) -> float:
    return amount_kwh * PRICE_PER_KWH

async def init_postgres():
    """Ініціалізація пулу підключень та централізоване створення всіх таблиць"""
    global db_pool
    db_url = os.getenv("DB_URL")
    
    if not db_url:
        logging.critical("💥 КРИТИЧНО: Змінну оточення DB_URL не знайдено!")
        raise ValueError("DB_URL variable is missing!")

    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        
    logging.info("⚙️ Спроба підключення до PostgreSQL...")
    try:
        db_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=10)
        logging.info("✅ Пул підключень до PostgreSQL успішно створено!")
        
        async with db_pool.acquire() as conn:
            # 1. Створення типів ENUM
            await conn.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status') THEN
                        CREATE TYPE payment_status AS ENUM ('pending', 'success', 'failed');
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_provider') THEN
                        CREATE TYPE payment_provider AS ENUM ('monobank', 'telegram');
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'transaction_type') THEN
                        CREATE TYPE transaction_type AS ENUM ('deposit', 'withdrawal', 'refund');
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
            
            # 3. Таблиця платежів (для інвойсів Monobank / Telegram)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    invoice_id VARCHAR(100) UNIQUE NOT NULL,
                    amount NUMERIC(10, 2) NOT NULL,
                    provider payment_provider NOT NULL,
                    status payment_status DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # 4. Ledger-таблиця кВт·год транзакцій
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS kw_transactions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    type transaction_type NOT NULL,
                    amount NUMERIC(10, 2) NOT NULL,
                    payment_id INTEGER REFERENCES payments(id),
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 5. Таблиця зарядних станцій (Централізовано тут)
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
            
            logging.info("📊 Усі таблиці та схеми PostgreSQL успішно верифіковано.")
    except Exception as e:
        logging.error(f"❌ Помилка ініціалізації PostgreSQL: {e}")
        raise e

async def close_postgres():
    """Закриття пулу підключень"""
    global db_pool
    if db_pool:
        await db_pool.close()
        logging.info("💤 Пул підключень до PostgreSQL закрито.")

async def get_user_data(user_id: int) -> Tuple[float, float]:
    """Отримання балансу та знижки за 1 атомарний запит (Прискорення х2)"""
    global db_pool
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO users (user_id, balance) VALUES ($1, 0.00)
            ON CONFLICT (user_id) DO UPDATE SET user_id = users.user_id
            RETURNING balance, discount;
        """, user_id)
        return float(row['balance']), float(row['discount'])

async def update_user_balance(user_id: int, amount_kwh: float, t_type: str = "deposit"):
    """Пряме Ledger-оновлення балансу користувача в кВт·год з безпечною транзакцією"""
    global db_pool
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
            if t_type == "deposit":
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount_kwh, user_id)
                await conn.execute("""
                    INSERT INTO kw_transactions (user_id, type, amount, description) 
                    VALUES ($1, 'deposit', $2, $3)
                """, user_id, amount_kwh, "Поповнення балансу (Ваучер / Admin)")
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
    """Створення інвойсу в очікуванні оплати"""
    global db_pool
    if not db_pool:
        logging.error("❌ Пул бази даних не ініціалізовано!")
        return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments (user_id, invoice_id, amount, provider, status)
            VALUES ($1, $2, $3, $4, 'pending')
        """, user_id, invoice_id, amount, provider)
        logging.info(f"📝 Створено рахунок {invoice_id} на sumu {amount} грн для користувача {user_id}")

async def save_station_to_local_db(station_id: str, name: str, address: str, connectors: str, lat: float, lon: float, operator: str):
    """Швидке збереження/оновлення інформації про станцію БЕЗ дублювання таблиць"""
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
        logging.error(f"⚠️ Не вдалося зберегти станцію {station_id} в локальну базу: {e}")

async def get_station_by_id(station_id: str) -> Tuple[str, str, str]:
    """Отримання даних станції з локальної бази з безпечним дефолтним резервом"""
    global db_pool
    if not db_pool:
        return "Тестовий Комплекс eVolt", "вулиця Зубра, 17", "Type 2, CCS 2"
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name, address, connectors FROM stations WHERE id = $1", station_id)
            if row:
                return row['name'], row['address'], row['connectors']
    except Exception as e:
        logging.error(f"❌ Помилка отримання станції {station_id}: {e}")
    return "Тестовий Комплекс eVolt", "вулиця Зубра, 17", "Type 2, CCS 2"
