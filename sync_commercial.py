import asyncio
import sqlite3
import logging
from app.services.ocpi.client import OCPIClient
from app.database.ocpi_repo import init_ocpi_tables, save_ocpi_location, save_ocpi_tariff, save_ocpi_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def run_full_sync():
    print("\n=== 🔄 ЗАПУСК ПОВНОЇ СИНХРОНІЗАЦІЇ КОМЕРЦІЙНИХ ДАНИХ БД 🔄 ===")
    init_ocpi_tables()
    client = OCPIClient()
    
    # 1. Handshake
    versions_res = await client.get_versions()
    if not versions_res: return
    version_221_url = versions_res["data"][0]["url"]
    
    details_res = await client.get_version_details(version_221_url)
    if not details_res: return
    
    urls = {ep["identifier"]: ep["url"] for ep in details_res["data"]["endpoints"]}
    
    # 2. Синхронізація Локацій
    if "locations" in urls:
        loc_data = await client.get_locations(urls["locations"])
        for loc in loc_data.get("data", []):
            save_ocpi_location(loc)
            
    # 3. Синхронізація Тарифів
    if "tariffs" in urls:
        tar_data = await client.get_tariffs(urls["tariffs"])
        for tariff in tar_data.get("data", []):
            save_ocpi_tariff(tariff)
            
    # 4. Синхронізація Сесій
    if "sessions" in urls:
        sess_data = await client.get_sessions(urls["sessions"])
        for sess in sess_data.get("data", []):
            save_ocpi_session(sess)
            
    print("\n=== 📊 ПЕРЕВІРКА НОВИХ ТАБЛИЦЬ У ФІЗИЧНІЙ БД ===")
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    
    # Перевіряємо тарифи
    tariffs = cursor.execute("SELECT id, price, currency FROM ocpi_tariffs").fetchall()
    print(f"Тарифи в БД: {tariffs}")
    
    # Перевіряємо сесії
    sessions = cursor.execute("SELECT id, kwh, total_cost, status FROM ocpi_sessions").fetchall()
    print(f"Активні сесії в БД: {sessions}")
    
    conn.close()
    print("===============================================================\n")

if __name__ == "__main__":
    asyncio.run(run_full_sync())
