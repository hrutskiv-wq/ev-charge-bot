import asyncio
import os
import sys

# Додаємо поточну директорію в шлях пошуку модулів Python
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database.connection import init_postgres, db_pool

async def main():
    print("🔗 Ініціалізація пулу підключень...")
    await init_postgres()
    
    if not db_pool:
        print("❌ Не вдалося ініціалізувати пул підключень!")
        return
        
    async with db_pool.acquire() as conn:
        print("✅ Зв'язок із PostgreSQL встановлено!")
        
        # Перевіряємо створені таблиці
        tables = await conn.fetch("""
            SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';
        """)
        
        print("\n📊 СТВОРЕНІ ТАБЛИЦІ В БАЗІ:")
        for t in tables:
            print(f"  🔹 {t['table_name']}")
            
        # Перевіряємо створені типи ENUM
        types = await conn.fetch("""
            SELECT t.typname FROM pg_type t 
            JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace 
            WHERE n.nspname = 'public' AND t.typtype = 'e';
        """)
        
        print("\n⚙️ СТВОРЕНІ ТИПИ ДАНИХ (ENUM):")
        for tp in types:
            print(f"  🔸 {tp['typname']}")
            
    await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())
