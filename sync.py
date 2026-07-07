import aiosqlite
import httpx
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()
OCM_KEY = os.getenv("OCM_KEY")

async def sync_ukraine_stations():
    # Наш URL для запиту станцій по Україні (Country Code: UA)
    API_URL = "https://api.openchargemap.io/v3/poi/"
    
    params = {
        "output": "json",
        "countrycode": "UA",
        "maxresults": 100,
        "key": OCM_KEY
    }
    
    print("⏳ Завантажуємо реальні станції України з Open Charge Map...")
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL, params=params)
            if response.status_code == 200:
                stations_data = response.json()
                
                async with aiosqlite.connect('users.db') as db:
                    # Переконуємось, що таблиця станцій існує з усіма потрібними полями
                    await db.execute('''
                        CREATE TABLE IF NOT EXISTS stations (
                            station_id TEXT PRIMARY KEY,
                            name TEXT,
                            address TEXT,
                            lat REAL,
                            lon REAL,
                            connectors TEXT
                        )
                    ''')
                    
                    count = 0
                    for poi in stations_data:
                        # Створюємо зрозумілий ID для водія (наприклад: OCM-194203)
                        station_id = f"OCM-{poi['ID']}"
                        
                        addr_info = poi.get('AddressInfo', {})
                        name = addr_info.get('Title', 'Зарядна станція')
                        address = addr_info.get('AddressLine1', 'Адреса відома')
                        lat = addr_info.get('Latitude')
                        lon = addr_info.get('Longitude')
                        
                        # Залишаємо поле connectors пустим, бо цей API не дає нам повної інфо
                        # Воно буде заповнюватися динамічно, коли користувач шукає станції поблизу
                        if lat and lon:
                            await db.execute('''
                                INSERT OR REPLACE INTO stations (station_id, name, address, lat, lon, connectors)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (station_id, name, address, lat, lon, ""))
                            count += 1
                    
                    await db.commit()
                
                print(f"✅ Успішно синхронізовано! У базу додано {count} реальних станцій України.")
            else:
                print(f"❌ Помилка сервера OCM. Статус коду: {response.status_code}")
            
    except Exception as e:
        print(f"❌ Сталася помилка під час запиту: {e}")

if __name__ == "__main__":
    asyncio.run(sync_ukraine_stations())
    