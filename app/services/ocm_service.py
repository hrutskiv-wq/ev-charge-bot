import os
import logging
import httpx
from aiocache import cached
from aiocache.serializers import JsonSerializer
from app.database.connection import save_station_to_local_db

OCM_KEY = os.getenv("OCM_KEY")

def location_key_builder(func, user_lat, user_lon, *args, **kwargs):
    rounded_lat = round(user_lat, 3)
    rounded_lon = round(user_lon, 3)
    return f"{func.__module__}:{func.__name__}:{rounded_lat}:{rounded_lon}"

@cached(
    ttl=300,
    key_builder=location_key_builder,
    serializer=JsonSerializer(),
    # Без цього @cached кешував би й None (помилка/timeout OCM API) на ті
    # самі 300с, що й успішний результат — один тимчасовий збій OCM API
    # "заморожував" би відповідь для цієї локації на 5 хв, навіть якщо API
    # відновлювалось за секунди. Кешуємо лише реальні (успішні) результати.
    skip_cache_func=lambda result: result is None,
)
async def find_three_nearest_stations(user_lat, user_lon):
    url = "https://api.openchargemap.io/v3/poi/"
    
    params = {
        "output": "json",
        "latitude": user_lat,
        "longitude": user_lon,
        "distance": 50,
        "distanceunit": "KM",
        "maxresults": 3
    }
    
    headers = {
        "X-API-Key": OCM_KEY if OCM_KEY else "",
        "User-Agent": "eVoltUA_Bot/1.0 (Contact: hrutskiv-wq)"
    }
    
    try:
        logging.info(f"Виконуємо запит до OCM API для координат: {user_lat}, {user_lon}")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=headers, timeout=8.0)
            
            if response.status_code != 200:
                logging.error(f"🚨 Помилка OCM API! Статус код: {response.status_code}. Текст: {response.text}")
                return None
                
            data = response.json()
            if not data:
                logging.info("OCM API повернув успішний порожній список (станцій немає в радіусі).")
                return []
                
            stations_list = []
            for poi in data:
                address_info = poi.get("AddressInfo", {})
                operator_info = poi.get("OperatorInfo", {})
                
                raw_id = poi.get("ID")
                station_id = f"OCM-{raw_id}"
                name = address_info.get("Title", "Без назви")
                address = address_info.get("AddressLine1", "Адреса не вказана")
                distance = address_info.get("Distance", 0.0)
                st_lat = address_info.get("Latitude")
                st_lon = address_info.get("Longitude")
                
                operator_name = operator_info.get("Title", "Невідомий оператор")
                if not operator_name or "Unknown" in operator_name or "Business" in operator_name:
                    operator_name = "Приватна/Муніципальна"
                    
                connections = poi.get("Connections", [])
                conn_list = []
                for c in connections:
                    conn_type = c.get("ConnectionType", {})
                    title = conn_type.get("Title", "Невідомий роз'єм")
                    title = title.replace(" (Socket Only)", "").replace(" (Tethered Cable)", "")
                    power = c.get("PowerKW")
                    quantity = c.get("Quantity")
                    
                    info_str = title
                    if power: 
                        info_str += f" ({power} кВт)"
                    if quantity and int(quantity) > 1: 
                        info_str += f" x{quantity}"
                    if info_str not in conn_list: 
                        conn_list.append(info_str)
                
                connectors_text = "; ".join(conn_list) if conn_list else "Інформація відсутня"
                
                # --- ВИПРАВЛЕНО: тепер передаємо всі 7 аргументів, включно з operator_name ---
                await save_station_to_local_db(station_id, name, address, connectors_text, st_lat, st_lon, operator_name)
                
                stations_list.append({
                    "id": station_id,
                    "name": name,
                    "address": address,
                    "distance": distance,
                    "operator": operator_name,
                    "connectors": connectors_text.replace("; ", ", "),
                    "lat": st_lat,
                    "lon": st_lon
                })
            
            return stations_list
            
    except httpx.TimeoutException:
        logging.error("OCM API Timeout (Перевищено час очікування)")
        return None
    except Exception as e:
        logging.error(f"Помилка в модулі OCM API: {e}", exc_info=True)
        return None
