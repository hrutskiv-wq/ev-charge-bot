import os
import logging
import httpx
from aiocache import cached
from aiocache.serializers import JsonSerializer
from app.database.connection import save_station_to_local_db

OCM_KEY = os.getenv("OCM_KEY")

def location_key_builder(func, user_lat, user_lon, *args, **kwargs):  #
    rounded_lat = round(user_lat, 3)
    rounded_lon = round(user_lon, 3)
    return f"{func.__module__}{func.__name__}:{rounded_lat}:{rounded_lon}"

@cached(ttl=300, key_builder=location_key_builder, serializer=JsonSerializer())  #
async def find_three_nearest_stations(user_lat, user_lon):
    url = "https://api.openchargemap.io/v3/poi/"
    params = {
        "output": "json",
        "latitude": user_lat,
        "longitude": user_lon,
        "distance": 25,          
        "distanceunit": "KM",
        "maxresults": 3,
        "key": OCM_KEY
    }
    
    try:
        logging.info(f"Виконуємо запит до OCM API для координат: {user_lat}, {user_lon}")
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=6.0) 
            if response.status_code != 200 or not response.json():
                return None
            
            stations_list = []
            for poi in response.json():
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
                if "Unknown" in operator_name or "Business" in operator_name:
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
                    if power: info_str += f" ({power} кВт)"
                    if quantity and int(quantity) > 1: info_str += f" x{quantity}"
                    if info_str not in conn_list: conn_list.append(info_str)
                
                connectors_text = "; ".join(conn_list) if conn_list else "Інформація відсутня"
                await save_station_to_local_db(station_id, name, address, connectors_text, st_lat, st_lon)
                
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
        logging.error("OCM API Timeout")
        return None
    except Exception as e:
        logging.error(f"Помилка OCM API: {e}")
        return None