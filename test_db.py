import asyncio
import os
import asyncpg

async def main():
    db_url = os.getenv("DB_URL")
    if db_url and db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        
    try:
        conn = await asyncpg.connect(db_url)
        
        # Отримуємо таблиці
        tables = await conn.fetch("""
            SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';
        """)
        print("\n📊 СТВОРЕНІ ТАБЛИЦІ В БАЗІ:")
        for t in tables:
            print(f"  🔹 {t['table_name']}")
            
        # Отримуємо типи ENUM
        types = await conn.fetch("""
            SELECT t.typname FROM pg_type t 
            JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace 
            WHERE n.nspname = 'public' AND t.typtype = 'e';
        """)
        print("\n⚙️ СТВОРЕНІ ТИПИ ДАНИХ (ENUM):")
        for tp in types:
            print(f"  🔸 {tp['typname']}")
            
        await conn.close()
    except Exception as e:
        print(f"❌ Помилка підключення: {e}")

if __name__ == "__main__":
    asyncio.run(main())
