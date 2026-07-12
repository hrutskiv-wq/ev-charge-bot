import sqlite3
import logging

DB_PATH = "users.db"
logger = logging.getLogger(__name__)

def init_ocpi_tables():
    """Створює повну реляційну структуру для всіх модулів OCPI 2.2.1"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Локації
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
    
    # 2. EVSE
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ocpi_evses (
        uid TEXT PRIMARY KEY,
        location_id TEXT,
        evse_id TEXT,
        status TEXT,
        FOREIGN KEY (location_id) REFERENCES ocpi_locations(id) ON DELETE CASCADE
    )
    """)
    
    # 3. Конектори
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

    # 4. ТАРИФИ
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ocpi_tariffs (
        id TEXT PRIMARY KEY,
        currency TEXT,
        price_type TEXT,
        price REAL,
        last_updated TEXT
    )
    """)

    # 5. СЕСІЇ ЗАРЯДЖАННЯ
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ocpi_sessions (
        id TEXT PRIMARY KEY,
        start_date_time TEXT,
        kwh REAL,
        auth_id TEXT,
        location_id TEXT,
        evse_uid TEXT,
        connector_id TEXT,
        currency TEXT,
        total_cost REAL,
        status TEXT,
        last_updated TEXT
    )
    """)
    
    conn.commit()
    conn.close()

def save_ocpi_location(location: dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("""
        INSERT OR REPLACE INTO ocpi_locations (id, type, name, address, city, country, latitude, longitude, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            location["id"], location["type"], location["name"], location["address"],
            location["city"], location["country"], 
            float(location["coordinates"]["latitude"]), float(location["coordinates"]["longitude"]),
            location["last_updated"]
        ))
        
        for evse in location.get("evses", []):
            cursor.execute("""
            INSERT OR REPLACE INTO ocpi_evses (uid, location_id, evse_id, status) 
            VALUES (?, ?, ?, ?)
            """, (evse["uid"], location["id"], evse["evse_id"], evse["status"]))
            
            for connector in evse.get("connectors", []):
                cursor.execute("""
                INSERT OR REPLACE INTO ocpi_connectors (id, evse_uid, standard, format, power_type, max_voltage, max_amperage)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    connector["id"], evse["uid"], connector["standard"], connector["format"],
                    connector["power_type"], connector["max_voltage"], connector["max_amperage"]
                ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Помилка БД локацій: {str(e)}")
    finally:
        conn.close()

def save_ocpi_tariff(tariff: dict):
    """Зберігає або оновлює комерційний тариф"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        comp = tariff["price_components"][0]
        cursor.execute("""
        INSERT OR REPLACE INTO ocpi_tariffs (id, currency, price_type, price, last_updated)
        VALUES (?, ?, ?, ?, ?)
        """, (tariff["id"], tariff["currency"], comp["type"], comp["price"], tariff["last_updated"]))
        conn.commit()
        logger.info(f"💾 Тариф {tariff['id']} успішно синхронізовано з БД.")
    except Exception as e:
        conn.rollback()
        logger.error(f"Помилка БД тарифів: {str(e)}")
    finally:
        conn.close()

def save_ocpi_session(session: dict):
    """Зберігає або оновлює активну сесію заряджання"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("""
        INSERT OR REPLACE INTO ocpi_sessions 
        (id, start_date_time, kwh, auth_id, location_id, evse_uid, connector_id, currency, total_cost, status, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session["id"], session["start_date_time"], session["kwh"], session["auth_id"],
            session["location_id"], session["evse_uid"], session["connector_id"],
            session["currency"], session["total_cost"], session["status"], session["last_updated"]
        ))
        conn.commit()
        logger.info(f"💾 Сесію {session['id']} успішно синхронізовано з БД.")
    except Exception as e:
        conn.rollback()
        logger.error(f"Помилка БД сесій: {str(e)}")
    finally:
        conn.close()
