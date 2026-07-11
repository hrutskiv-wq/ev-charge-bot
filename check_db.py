import asyncio
import os
import asyncpg
from dotenv import load_dotenv

# Завантажуємо змінні з .env файлу проєкту
load_dotenv()

async def main():
    # Беремо URL підключення, який використовує твій бот
    database_url = os.getenv("DATABASE_URL")
    print(f"🔗 Спроба підключення до: {database_url}")
    
    try:
        conn = await asyncpg.connect(database_url)
        print("✅ Успішно підключено до PostgreSQL!")
        
        # Запит на список усіх таблиць
        rows = await conn.fetch("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public';
        """)
        
        print("\n📊 Знайдені таблиці в базі даних:")
        if not rows:
            print("❌ Таблиць немає (база порожня).")
        else:
            for row in rows:
                print(f"🔹 {row['table_name']}")
                
        await conn.close()
    except Exception as e:
        print(f"💥 Помилка: {e}")

if __name__ == "__main__":
    asyncio.run(main())
