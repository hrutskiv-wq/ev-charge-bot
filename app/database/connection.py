import os
import logging
import asyncio  # 💥 ДОДАЙ ЦЕЙ РЯДОК, щоб запрацював sleep()
import asyncpg

PRICE_PER_KWH = 15.0  # Вартість 1 кВт для конвертації

def uah_to_kwh(amount_uah): return amount_uah / PRICE_PER_KWH
def kwh_to_uah(amount_kwh): return amount_kwh * PRICE_PER_KWH

# Глобальний пул підключень
db_pool = None

async def init_postgres():
    """Ініціалізація пулу підключень із механізмом повторних спроб для Docker"""
    global db_pool
    db_url = os.getenv("DB_URL")
    
    if db_url and db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        
    if not db_url:
        logging.error("❌ Змінну оточення DB_URL не знайдено!")
        return

    # Робимо до 5 спроб підключення з паузою, щоб дати Postgres час завантажитися
    retries = 5
    for attempt in range(1, retries + 1):
        try:
            logging.info(f"Спроба підключення до PostgreSQL {attempt}/{retries}...")
            db_pool = await asyncpg.create_pool(dsn=db_url, min_size=2, max_size=10)
            logging.info("✅ Пул підключень до PostgreSQL успішно створено!")
            
            # Створюємо таблиці
            await create_tables()
            return  # Успішно підключилися, виходимо з функції
            
        except Exception as e:
            logging.warning(f"⚠️ База даних ще не готова (спроба {attempt}): {e}")
            if attempt < retries:
                await asyncio.sleep(3)  # Чекаємо 3 секунди перед наступною спробою
            else:
                logging.error("💥 Не вдалося підключитися до PostgreSQL після всіх спроб!")
                raise e  # Кидаємо помилку далі, щоб Docker перезапустив контейнер бота

async def create_tables():
    """Створення базових таблиць + нової Ledger-архітектури білінгу"""
    global db_pool
    if not db_pool:
        return
        
    async with db_pool.acquire() as conn:
        # --- 0. Створення ENUM типів для білінгу ---
        await conn.execute("""
            DO $$ 
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status') THEN
                    CREATE TYPE payment_status AS ENUM ('pending', 'success', 'failed', 'refunded');
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_provider') THEN
                    CREATE TYPE payment_provider AS ENUM ('liqpay', 'monobank');
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'transaction_type') THEN
                    CREATE TYPE transaction_type AS ENUM ('deposit', 'withdrawal', 'bonus', 'correction');
                END IF;
            END $$;
        """)

        # --- 1. Базові таблиці проєкту ---
        # Таблиця користувачів (BIGINT ідеально підходить для великих Telegram ID)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance NUMERIC(10, 2) DEFAULT 0.0,
                discount NUMERIC(3, 2) DEFAULT 1.0
            );
        """)
        
        # Таблиця станцій
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
        
        # Стара таблиця транзакцій (залишаємо для сумісності)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                amount NUMERIC(10, 2),
                type VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # --- 2. НОВІ ТАБЛИЦІ БІЛІНГУ ---
        # Нова таблиця фіксації рахунків / інвойсів
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                invoice_id VARCHAR(100) UNIQUE NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                provider payment_provider NOT NULL,
                status payment_status DEFAULT 'pending',
                payload JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Журнал балансу електроенергії (кВт·год) Ledger
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kw_transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                type transaction_type NOT NULL,
                amount NUMERIC(8, 2) NOT NULL,
                payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
                session_id INTEGER,
                description TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Створення високопродуктивних індексів
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_kw_transactions_user ON kw_transactions(user_id);")

        logging.info("📊 Усі таблиці PostgreSQL (включаючи білінг) успішно перевірені/створені!")


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


# --- НОВА ФУНКЦІЯ ДЛЯ СТВОРЕННЯ РАХУНКУ ПЕРЕД ОПЛАТОЮ ---
async def create_pending_payment(user_id: int, invoice_id: str, amount: float, provider: str = "monobank"):
    """
    Реєструє новий рахунок у таблиці payments зі статусом 'pending'
    """
    global db_pool
    if not db_pool:
        logging.error("❌ Пул бази даних не ініціалізовано!")
        return
        
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments (user_id, invoice_id, amount, provider, status)
            VALUES ($1, $2, $3, $4, 'pending')
        """, user_id, invoice_id, amount, provider)
        logging.info(f"📝 Створено рахунок {invoice_id} на суму {amount} грн для користувача {user_id} ({provider})")