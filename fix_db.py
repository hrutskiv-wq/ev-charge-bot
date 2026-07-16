from app.database import connection
import asyncio

async def fix():
    await connection.init_postgres()
    async with connection.db_pool.acquire() as c:
        # Оновлюємо записи з 12345 на ваш реальний ID 514533557
        result = await c.execute("UPDATE kw_transactions SET user_id = 514533557 WHERE user_id = 12345")
        print(f"Результат операції: {result}")

asyncio.run(fix())
