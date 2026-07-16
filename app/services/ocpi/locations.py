import json
from datetime import datetime
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Request, HTTPException, status
from pydantic import BaseModel

router = APIRouter(prefix="/ocpi/cpo/2.2.1/locations", tags=["OCPI Locations"])

# ---------------------------------------------------------
# Pydantic-схеми для валідації вхідного OCPI 2.2.1 JSON
# ---------------------------------------------------------

class ConnectorSchema(BaseModel):
    id: str
    standard: str
    format: str
    power_type: str
    max_voltage: int
    max_amperage: int
    max_electric_power: int
    tariff_ids: Optional[List[str]] = None
    terms_and_conditions: Optional[str] = None
    last_updated: datetime

class EvseSchema(BaseModel):
    uid: str
    evse_id: Optional[str] = None
    status: str
    status_schedule: Optional[List[dict]] = None
    capabilities: Optional[List[str]] = None
    floor_level: Optional[str] = None
    coordinates: Optional[dict] = None
    physical_reference: Optional[str] = None
    directions: Optional[List[dict]] = None
    parking_restrictions: Optional[List[str]] = None
    images: Optional[List[dict]] = None
    connectors: List[ConnectorSchema]
    last_updated: datetime

class LocationSchema(BaseModel):
    id: str
    publish: bool = True
    name: Optional[str] = None
    address: str
    city: str
    postal_code: Optional[str] = None
    country: str
    coordinates: dict  # {"latitude": "...", "longitude": "..."}
    type: str = "UNKNOWN"
    parking_type: Optional[str] = None
    opening_times: Optional[dict] = None
    images: Optional[List[dict]] = None
    energy_mix: Optional[dict] = None
    directions: Optional[List[dict]] = None
    operator: Optional[dict] = None
    suboperator: Optional[dict] = None
    owner: Optional[dict] = None
    time_zone: Optional[str] = None
    evses: Optional[List[EvseSchema]] = None
    last_updated: datetime

# ---------------------------------------------------------
# Асинхронний роутер для обробки Webhook (CPO -> eMSP)
# ---------------------------------------------------------

@router.put("/{country_code}/{party_id}/{location_id}", status_code=status.HTTP_200_OK)
async def upsert_location(
    country_code: str,
    party_id: str,
    location_id: str,
    location_data: LocationSchema,
    request: Request
):
    pool = request.app.state.db_pool
    bot = getattr(request.app.state, "bot", None)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Перевіряємо або створюємо партнера (Party)
            party = await conn.fetchrow(
                "SELECT id FROM parties WHERE country_code = $1 AND party_id = $2",
                country_code.upper(), party_id.upper()
            )
            if not party:
                party_uuid = await conn.value(
                    """
                    INSERT INTO parties (party_id, country_code, name)
                    VALUES ($1, $2, $3)
                    RETURNING id
                    """,
                    party_id.upper(), country_code.upper(), f"CPO {party_id.upper()}"
                )
            else:
                party_uuid = party["id"]

            # 2. Робимо UPSERT для Location
            lat = float(location_data.coordinates.get("latitude"))
            lng = float(location_data.coordinates.get("longitude"))

            location_uuid = await conn.value(
                """
                INSERT INTO locations (
                    party_id, cpo_location_id, publish, name, address, city, postal_code, country,
                    latitude, longitude, type, parking_type, opening_times, images, energy_mix,
                    directions, operator, suboperator, owner, time_zone, last_updated
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21)
                ON CONFLICT (party_id, cpo_location_id) DO UPDATE SET
                    publish = EXCLUDED.publish,
                    name = EXCLUDED.name,
                    address = EXCLUDED.address,
                    city = EXCLUDED.city,
                    postal_code = EXCLUDED.postal_code,
                    country = EXCLUDED.country,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    type = EXCLUDED.type,
                    parking_type = EXCLUDED.parking_type,
                    opening_times = EXCLUDED.opening_times,
                    images = EXCLUDED.images,
                    energy_mix = EXCLUDED.energy_mix,
                    directions = EXCLUDED.directions,
                    operator = EXCLUDED.operator,
                    suboperator = EXCLUDED.suboperator,
                    owner = EXCLUDED.owner,
                    time_zone = EXCLUDED.time_zone,
                    last_updated = EXCLUDED.last_updated
                RETURNING id
                """,
                party_uuid,
                location_id,
                location_data.publish,
                location_data.name,
                location_data.address,
                location_data.city,
                location_data.postal_code,
                location_data.country.upper(),
                lat,
                lng,
                location_data.type,
                location_data.parking_type,
                json.dumps(location_data.opening_times) if location_data.opening_times else None,
                json.dumps(location_data.images) if location_data.images else None,
                json.dumps(location_data.energy_mix) if location_data.energy_mix else None,
                json.dumps(location_data.directions) if location_data.directions else None,
                json.dumps(location_data.operator) if location_data.operator else None,
                json.dumps(location_data.suboperator) if location_data.suboperator else None,
                json.dumps(location_data.owner) if location_data.owner else None,
                location_data.time_zone,
                location_data.last_updated
            )

            # 3. Обробляємо список EVSEs та Connectors
            if location_data.evses:
                await conn.execute("DELETE FROM evses WHERE location_id = $1", location_uuid)

                for evse in location_data.evses:
                    evse_lat = float(evse.coordinates.get("latitude")) if evse.coordinates else None
                    evse_lng = float(evse.coordinates.get("longitude")) if evse.coordinates else None

                    evse_uuid = await conn.value(
                        """
                        INSERT INTO evses (
                            location_id, evse_uid, evse_id, status, status_schedule, capabilities,
                            floor_level, latitude, longitude, physical_reference, directions,
                            parking_restrictions, images, last_updated
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                        RETURNING id
                        """,
                        location_uuid,
                        evse.uid,
                        evse.evse_id,
                        evse.status,
                        json.dumps(evse.status_schedule) if evse.status_schedule else None,
                        json.dumps(evse.capabilities) if evse.capabilities else None,
                        evse.floor_level,
                        evse_lat,
                        evse_lng,
                        evse.physical_reference,
                        json.dumps(evse.directions) if evse.directions else None,
                        json.dumps(evse.parking_restrictions) if evse.parking_restrictions else None,
                        json.dumps(evse.images) if evse.images else None,
                        evse.last_updated
                    )

                    # 4. Вставляємо конектори для кожного EVSE
                    for connector in evse.connectors:
                        await conn.execute(
                            """
                            INSERT INTO connectors (
                                evse_id, connector_id, standard, format, power_type,
                                max_voltage, max_amperage, max_electric_power,
                                tariff_ids, terms_and_conditions, last_updated
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                            """,
                            evse_uuid,
                            connector.id,
                            connector.standard,
                            connector.format,
                            connector.power_type,
                            connector.max_voltage,
                            connector.max_amperage,
                            connector.max_electric_power,
                            json.dumps(connector.tariff_ids) if connector.tariff_ids else None,
                            connector.terms_and_conditions,
                            connector.last_updated
                        )

    # 5. Telegram-сповіщення через бота в адмін-чат
    if bot:
        try:
            chat_id = request.app.state.logs_chat_id
            msg = f"🔌 *OCPI 2.2.1: Нова локація!*\n" \
                  f"📍 *Назва:* {location_data.name or 'Без назви'}\n" \
                  f"🏢 *Місто:* {location_data.city} ({location_data.country})\n" \
                  f"⚡ *Кількість EVSE:* {len(location_data.evses) if location_data.evses else 0}"
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            print(f"Telegram notification failed: {e}")

    return {"status_code": 1000, "status_message": "Success", "timestamp": datetime.utcnow().isoformat()}
