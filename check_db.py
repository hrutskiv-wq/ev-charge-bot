from app.database import connection
import asyncio

async def main():
    try:
        await connection.init_postgres()
        if connection.db_pool is None:
            print("DB_POOL IS NONE")
            return
            
        async with connection.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM kw_transactions WHERE user_id = 12345")
            if not rows:
                print("Немає записів для користувача 12345.")
            else:
                print("Знайдені записи:")
                for r in rows:
                    print(dict(r))
    except Exception as e:
        print(f"Помилка: {e}")

asyncio.run(main())
