import sqlite3
import logging

DB_PATH = "users.db"
logger = logging.getLogger(__name__)

def init_ocpi_tables():
    """Створює реляційну структуру для OCPI модулів, якщо вона відсутня"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Таблиця локацій (самих станцій)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ocpi_locations (
        id TEXT PRIMARY KEY,
        type TEXT,
        name TEXT,
        address TEXT,
        city TEXT,
        country TEXT,
        latitude REAL,
        longitude REAL,
        last_updated TEXT
    )
    """)
    
    # 2. Таблиця EVSE (конкретних точок/шаф підключення на локації)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ocpi_evses (
        uid TEXT PRIMARY KEY,
        location_id TEXT,
        evse_id TEXT,
        status TEXT,
        FOREIGN KEY (location_id) REFERENCES ocpi_locations(id) ON DELETE CASCADE
    )
    """)
    
    # 3. Таблиця конекторів (конкретних кабелів/портів на EVSE)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ocpi_connectors (
        id TEXT,
        evse_uid TEXT,
        standard TEXT,
        format TEXT,
        power_type TEXT,
        max_voltage INTEGER,
        max_amperage INTEGER,
        PRIMARY KEY (id, evse_uid),
        FOREIGN KEY (evse_uid) REFERENCES ocpi_evses(uid) ON DELETE CASCADE
    )
    """)
    
    conn.commit()
    conn.close()

def save_ocpi_location(location: dict):
    """Зберігає або повністю оновлює (Upsert) дані станції та її портів"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        # 1. Записуємо/оновлюємо локацію
        cursor.execute("""
        INSERT OR REPLACE INTO ocpi_locations (id, type, name, address, city, country, latitude, longitude, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            location["id"], location["type"], location["name"], location["address"],
            location["city"], location["country"], 
            float(location["coordinates"]["latitude"]), float(location["coordinates"]["longitude"]),
            location["last_updated"]
        ))
        
        # 2. Записуємо/оновлюємо точки EVSE
        for evse in location.get("evses", []):
            cursor.execute("""
            INSERT OR REPLACE INTO ocpi_evses (uid, location_id, evse_id, status)
            VALUES (?, ?, ?, ?)
            """, (evse["uid"], location["id"], evse["evse_id"], evse["status"]))
            
            # 3. Записуємо/оновлюємо конектори для цього EVSE
            for connector in evse.get("connectors", []):
                cursor.execute("""
                INSERT OR REPLACE INTO ocpi_connectors (id, evse_uid, standard, format, power_type, max_voltage, max_amperage)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    connector["id"], evse["uid"], connector["standard"], connector["format"],
                    connector["power_type"], connector["max_voltage"], connector["max_amperage"]
                ))
                
        conn.commit()
        logger.info(f"💾 Станцію {location['id']} успішно збережено/оновлено в базі даних.")
    except Exception as e:
        conn.rollback()
        logger.error(f"❌ Помилка запису станції в БД: {str(e)}")
    finally:
        conn.close()
