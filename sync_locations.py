import asyncio
import sqlite3
import logging
from app.services.ocpi.client import OCPIClient
from app.database.ocpi_repo import init_ocpi_tables, save_ocpi_location

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def run_sync():
    print("\n=== 🔄 ЗАПУСК СИНХРОНІЗАЦІЇ OCPI -> DATABASE 🔄 ===")
    
    # Ініціалізуємо таблиці в базі даних
    init_ocpi_tables()
    
    client = OCPIClient()
    
    # 1. Крокуємо по Handshake
    versions_res = await client.get_versions()
    if not versions_res: return
    
    version_221_url = versions_res["data"][0]["url"]
    details_res = await client.get_version_details(version_221_url)
    if not details_res: return
    
    locations_url = None
    for ep in details_res["data"]["endpoints"]:
        if ep["identifier"] == "locations":
            locations_url = ep["url"]
            
    if not locations_url:
        print("❌ Не знайдено ендпоінт локацій.")
        return
        
    # 2. Отримуємо дані з сервера оператора
    locations_data = await client.get_locations(locations_url)
    if not locations_data or "data" not in locations_data:
        print("❌ Немає даних для синхронізації.")
        return
        
    # 3. Записуємо кожну отриману станцію в базу даних
    for loc in locations_data["data"]:
        save_ocpi_location(loc)
        
    print("\n=== 📊 ПЕРЕВІРКА ФІЗИЧНОГО ЗБЕРЕЖЕННЯ В ТАБЛИЦЯХ БД ===")
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    
    # Перевіряємо локації
    locs = cursor.execute("SELECT id, name, city FROM ocpi_locations").fetchall()
    print(f"Знайдено локацій в БД: {locs}")
    
    # Перевіряємо EVSE та статуси
    evses = cursor.execute("SELECT uid, status FROM ocpi_evses").fetchall()
    print(f"Знайдено точок EVSE в БД (статуси): {evses}")
    
    # Перевіряємо конектори
    connectors = cursor.execute("SELECT id, standard, power_type FROM ocpi_connectors").fetchall()
    print(f"Знайдено конекторів в БД: {connectors}")
    
    conn.close()
    print("=========================================================\n")

if __name__ == "__main__":
    asyncio.run(run_sync())
