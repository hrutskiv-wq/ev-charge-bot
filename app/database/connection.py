import aiosqlite

PRICE_PER_KWH = 15.0  # Вартість 1 кВт для конвертації

def uah_to_kwh(amount_uah): return amount_uah / PRICE_PER_KWH  #
def kwh_to_uah(amount_kwh): return amount_kwh * PRICE_PER_KWH  #

class Database:
    def __init__(self, db_path='users.db'):
        self.db_path = db_path

    async def execute_commit(self, query, params=()):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(query, params)
            await db.commit()

    async def fetchone(self, query, params=()):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, query, params=()):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                return await cursor.fetchall()

db = Database()

async def initialize_db():
    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, discount REAL DEFAULT 1.0)')
        await conn.execute('''
                CREATE TABLE IF NOT EXISTS stations (
                    station_id TEXT PRIMARY KEY, name TEXT, address TEXT, lat REAL, lon REAL, connectors TEXT
                )
            ''')
        await conn.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    type TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        await conn.commit()

async def get_user_data(user_id):
    result = await db.fetchone('SELECT balance, discount FROM users WHERE user_id = ?', (user_id,))
    return result if result else (0.0, 1.0)

async def save_station_to_local_db(station_id, name, address, connectors, lat=0.0, lon=0.0):  #
    await db.execute_commit('''
        INSERT OR REPLACE INTO stations (station_id, name, address, connectors, lat, lon) 
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (station_id, name, address, connectors, lat, lon))

async def get_station_by_id(station_id):  #
    return await db.fetchone('SELECT name, address, connectors FROM stations WHERE station_id = ?', (station_id,))

async def log_transaction(user_id, amount, t_type):  #
    await db.execute_commit(
        'INSERT INTO transactions (user_id, amount, type) VALUES (?, ?, ?)',
        (user_id, amount, t_type)
    )

async def update_user_balance(user_id, amount_uah, t_type="deposit"):  #
    await db.execute_commit('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount_uah, user_id))
    await log_transaction(user_id, amount_uah, t_type)

async def set_user_discount(user_id, discount_value):  #
    await db.execute_commit('UPDATE users SET discount = ? WHERE user_id = ?', (discount_value, user_id))