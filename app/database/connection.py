import os
import logging
import asyncpg

PRICE_PER_KWH = 15.0  # Вартість 1 кВт для конвертації

def uah_to_kwh(amount_uah): return amount_uah / PRICE_PER_KWH
def kwh_to_uah(amount_kwh): return amount_kwh * PRICE_PER_KWH

# Глобальний пул підключень
db_pool = None

async def init_postgres():
    """Ініціалізація пулу підключень та створення таблиць при старті бота"""
    global db_pool
    db_url = os.getenv("DB_URL")
    
    # Коригуємо префікс для драйвера asyncpg
    if db_url and db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        
    if not db_url:
        logging.error("❌ Змінну оточення DB_URL не знайдено!")
        return

    try:
        logging.info("Підключення до бази даних PostgreSQL...")
        db_pool = await asyncpg.create_pool(dsn=db_url, min_size=2, max_size=10)
        logging.info("✅ Пул підключень до PostgreSQL успішно створено!")
        
        # Створюємо таблиці, якщо їх немає
        await create_tables()
    except Exception as e:
        logging.error(f"💥 Помилка ініціалізації PostgreSQL: {e}", exc_info=True)


async def create_tables():
    """Створення таблиць (аналог вашого initialize_db)"""
    global db_pool
    if not db_pool:
        return
        
    async with db_pool.acquire() as conn:
        # 1. Таблиця користувачів (BIGINT ідеально підходить для великих Telegram ID)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance NUMERIC(10, 2) DEFAULT 0.0,
                discount NUMERIC(3, 2) DEFAULT 1.0
            );
        """)
        
        # 2. Таблиця станцій
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stations (
                station_id VARCHAR(50) PRIMARY KEY,
                name VARCHAR(255),
                address TEXT,
                lat DOUBLE PRECISION DEFAULT 0.0,
                lon DOUBLE PRECISION DEFAULT 0.0,
                connectors TEXT
            );
        """)
        
        # 3. Таблиця транзакцій (SERIAL замість AUTOINCREMENT)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                amount NUMERIC(10, 2),
                type VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        logging.info("📊 Усі таблиці PostgreSQL успішно перевірені/створені!")


async def close_postgres():
    """Закриття пулу при зупинці бота"""
    global db_pool
    if db_pool:
        await db_pool.close()
        logging.info("🔒 Пул підключень до PostgreSQL закритий.")


# ==========================================
# БІЗНЕС-ЛОГІКА (ВАШІ ФУНКЦІЇ, АДАПТОВАНІ ПІД POSTGRES)
# ==========================================

async def get_user_data(user_id: int):
    """Отримання балансу та знижки водія"""
    global db_pool
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT balance, discount FROM users WHERE user_id = $1', user_id)
        if not row:
            # Якщо водія немає в базі, автоматично створюємо його з дефолтними значеннями
            await conn.execute('INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING', user_id)
            return 0.0, 1.0
        return float(row['balance']), float(row['discount'])


async def save_station_to_local_db(station_id, name, address, connectors, lat=0.0, lon=0.0):
    """Збереження або оновлення даних станції (Аналог INSERT OR REPLACE)"""
    global db_pool
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO stations (station_id, name, address, connectors, lat, lon) 
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (station_id) 
            DO UPDATE SET name = $2, address = $3, connectors = $4, lat = $5, lon = $6
        ''', station_id, name, address, connectors, lat, lon)


async def get_station_by_id(station_id):
    """Пошук станції за її ID"""
    global db_pool
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT name, address, connectors FROM stations WHERE station_id = $1', station_id)
        return (row['name'], row['address'], row['connectors']) if row else None


async def log_transaction(user_id, amount, t_type):
    """Логування фінансової операції в базу"""
    global db_pool
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO transactions (user_id, amount, type) VALUES ($1, $2, $3)',
            user_id, amount, t_type
        )


async def update_user_balance(user_id, amount_uah, t_type="deposit"):
    """Зміна балансу користувача + логування транзакції"""
    global db_pool
    async with db_pool.acquire() as conn:
        # Перевіряємо, чи є користувач, щоб уникнути помилки foreign key
        await conn.execute('INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING', user_id)
        
        await conn.execute('UPDATE users SET balance = balance + $1 WHERE user_id = $2', amount_uah, user_id)
        await log_transaction(user_id, amount_uah, t_type)


async def set_user_discount(user_id, discount_value):
    """Встановлення індивідуальної знижки для водія"""
    global db_pool
    async with db_pool.acquire() as conn:
        await conn.execute('INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING', user_id)
        await conn.execute('UPDATE users SET discount = $1 WHERE user_id = $2', discount_value, user_id)