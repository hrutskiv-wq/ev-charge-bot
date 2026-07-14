import logging
from app.database.connection import get_db_pool

logger = logging.getLogger(__name__)

async def init_ocpi_tables():
    """Створює повну реляційну структуру для всіх модулів OCPI 2.2.1 у PostgreSQL"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # 1. Локації OCPI
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS ocpi_locations (
            id VARCHAR(100) PRIMARY KEY,
            type VARCHAR(50),
            name VARCHAR(255),
            address TEXT,
            city VARCHAR(100),
            country VARCHAR(100),
            latitude NUMERIC(9,6),
            longitude NUMERIC(9,6),
            last_updated VARCHAR(100)
        );
        """)
        
        # 2. EVSE (Точки зарядки на локаціях)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS ocpi_evses (
            uid VARCHAR(100) PRIMARY KEY,
            location_id VARCHAR(100) REFERENCES ocpi_locations(id) ON DELETE CASCADE,
            evse_id VARCHAR(100),
            status VARCHAR(50)
        );
        """)

        # 3. Конектори на EVSE
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS ocpi_connectors (
            id VARCHAR(100) PRIMARY KEY,
            evse_uid VARCHAR(100) REFERENCES ocpi_evses(uid) ON DELETE CASCADE,
            standard VARCHAR(100),
            format VARCHAR(50),
            power_type VARCHAR(50),
            max_voltage INTEGER,
            max_amperage INTEGER
        );
        """)

        # 4. Тарифи OCPI
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS ocpi_tariffs (
            id VARCHAR(100) PRIMARY KEY,
            currency VARCHAR(10),
            price_type VARCHAR(50),
            price NUMERIC(10, 4),
            last_updated VARCHAR(100)
        );
        """)

        # 5. Сесії OCPI
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS ocpi_sessions (
            id VARCHAR(100) PRIMARY KEY,
            location_id VARCHAR(100) REFERENCES ocpi_locations(id) ON DELETE SET NULL,
            evse_uid VARCHAR(100) REFERENCES ocpi_evses(uid) ON DELETE SET NULL,
            connector_id VARCHAR(100),
            status VARCHAR(50),
            kwh NUMERIC(10, 4),
            amount NUMERIC(10, 2),
            currency VARCHAR(10),
            last_updated VARCHAR(100)
        );
        """)
        logger.info("✅ Усі таблиці OCPI у PostgreSQL успішно ініціалізовано!")

async def save_ocpi_location(location: dict):
    """Зберігає або оновлює локацію OCPI, її EVSE та конектори в PostgreSQL"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Зберігаємо саму локацію
            await conn.execute("""
                INSERT INTO ocpi_locations (id, type, name, address, city, country, latitude, longitude, last_updated)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (id) DO UPDATE SET
                    type = EXCLUDED.type,
                    name = EXCLUDED.name,
                    address = EXCLUDED.address,
                    city = EXCLUDED.city,
                    country = EXCLUDED.country,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    last_updated = EXCLUDED.last_updated;
            """, 
            location.get('id'), 
            location.get('type'), 
            location.get('name'), 
            location.get('address'), 
            location.get('city'), 
            location.get('country'), 
            location.get('latitude'), 
            location.get('longitude'), 
            location.get('last_updated'))

            # 2. Зберігаємо вкладені EVSE та Конектори
            for evse in location.get('evses', []):
                await conn.execute("""
                    INSERT INTO ocpi_evses (uid, location_id, evse_id, status)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (uid) DO UPDATE SET
                        location_id = EXCLUDED.location_id,
                        evse_id = EXCLUDED.evse_id,
                        status = EXCLUDED.status;
                """, evse.get('uid'), location.get('id'), evse.get('evse_id'), evse.get('status'))

                for conn_data in evse.get('connectors', []):
                    await conn.execute("""
                        INSERT INTO ocpi_connectors (id, evse_uid, standard, format, power_type, max_voltage, max_amperage)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (id) DO UPDATE SET
                            evse_uid = EXCLUDED.evse_uid,
                            standard = EXCLUDED.standard,
                            format = EXCLUDED.format,
                            power_type = EXCLUDED.power_type,
                            max_voltage = EXCLUDED.max_voltage,
                            max_amperage = EXCLUDED.max_amperage;
                    """, 
                    conn_data.get('id'), 
                    evse.get('uid'), 
                    conn_data.get('standard'), 
                    conn_data.get('format'), 
                    conn_data.get('power_type'), 
                    conn_data.get('max_voltage'), 
                    conn_data.get('max_amperage'))

async def save_ocpi_tariff(tariff: dict):
    """Зберігає або оновлює тариф OCPI в PostgreSQL"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO ocpi_tariffs (id, currency, price_type, price, last_updated)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO UPDATE SET
                currency = EXCLUDED.currency,
                price_type = EXCLUDED.price_type,
                price = EXCLUDED.price,
                last_updated = EXCLUDED.last_updated;
        """, 
        tariff.get('id'), 
        tariff.get('currency'), 
        tariff.get('price_type'), 
        tariff.get('price'), 
        tariff.get('last_updated'))

async def save_ocpi_session(session: dict):
    """Зберігає або оновлює сесію OCPI в PostgreSQL"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO ocpi_sessions (id, location_id, evse_uid, connector_id, status, kwh, amount, currency, last_updated)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (id) DO UPDATE SET
                location_id = EXCLUDED.location_id,
                evse_uid = EXCLUDED.evse_uid,
                connector_id = EXCLUDED.connector_id,
                status = EXCLUDED.status,
                kwh = EXCLUDED.kwh,
                amount = EXCLUDED.amount,
                currency = EXCLUDED.currency,
                last_updated = EXCLUDED.last_updated;
        """, 
        session.get('id'), 
        session.get('location_id'), 
        session.get('evse_uid'), 
        session.get('connector_id'), 
        session.get('status'), 
        session.get('kwh'), 
        session.get('amount'), 
        session.get('currency'), 
        session.get('last_updated'))
