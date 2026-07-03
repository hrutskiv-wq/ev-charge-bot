import sqlite3
import requests

def sync_ukraine_stations():
    # Наш URL для запиту станцій по Україні (Country Code: UA)
    API_URL = "https://api.openchargemap.io/v3/poi/"
    API_KEY = "a2af00f8-6c92-458a-bafb-08aa527a77c7"
    
    params = {
        "output": "json",
        "countrycode": "UA",
        "maxresults": 100,
        "key": API_KEY
    }
    
    print("⏳ Завантажуємо реальні станції України з Open Charge Map...")
    
    try:
        response = requests.get(API_URL, params=params)
        if response.status_code == 200:
            stations_data = response.json()
            
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            
            # Переконуємось, що таблиця станцій існує
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stations (
                    station_id TEXT PRIMARY KEY,
                    name TEXT,
                    address TEXT,
                    lat REAL,
                    lon REAL
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
                
                if lat and lon:
                    cursor.execute('''
                        INSERT OR REPLACE INTO stations (station_id, name, address, lat, lon)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (station_id, name, address, lat, lon))
                    count += 1
            
            conn.commit()
            conn.close()
            print(f"✅ Успішно синхронізовано! У базу додано {count} реальних станцій України.")
        else:
            print(f"❌ Помилка сервера OCM. Статус коду: {response.status_code}")
            
    except Exception as e:
        print(f"❌ Сталася помилка під час запиту: {e}")

if __name__ == "__main__":
    sync_ukraine_stations()
    