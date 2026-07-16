from app.database import connection
import asyncio

async def add_deposit():
    # 1. Ініціалізуємо підключення
    await connection.init_postgres()
    
    # 2. Додаємо запис
    async with connection.db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO kw_transactions (user_id, type, amount, description) 
            VALUES ($1, $2, $3, $4)
        """, 12345, deposit, 100.0, Тестове