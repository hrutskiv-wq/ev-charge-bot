from fastapi import FastAPI
from datetime import datetime, timezone
import uvicorn

app = FastAPI(title="OCPI CPO Mock Server")

@app.get("/ocpi/versions")
async def get_versions():
    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": [
            {
                "version": "2.2.1",
                "url": "http://127.0.0.1:8080/ocpi/2.2.1"
            }
        ]
    }

@app.get("/ocpi/2.2.1")
async def get_version_details():
    """Повертаємо список усіх трьох активованих бізнес-модулів"""
    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": {
            "version": "2.2.1",
            "endpoints": [
                {
                    "identifier": "locations",
                    "role": "SENDER",
                    "url": "http://127.0.0.1:8080/ocpi/cpo/2.2.1/locations"
                },
                {
                    "identifier": "tariffs",
                    "role": "SENDER",
                    "url": "http://127.0.0.1:8080/ocpi/cpo/2.2.1/tariffs"
                },
                {
                    "identifier": "sessions",
                    "role": "SENDER",
                    "url": "http://127.0.0.1:8080/ocpi/cpo/2.2.1/sessions"
                }
            ]
        }
    }

@app.get("/ocpi/cpo/2.2.1/locations")
async def get_locations():
    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": [
            {
                "id": "LOC-001",
                "type": "ON_STREET",
                "name": "Ево-Заряд Зубра Центр",
                "address": "вулиця Івана Франка, 12",
                "city": "Зубра",
                "country": "UKR",
                "coordinates": {"latitude": "49.7753", "longitude": "24.0512"},
                "evses": [
                    {
                        "uid": "EVSE-001",
                        "evse_id": "UA*EVO*E001",
                        "status": "AVAILABLE",
                        "connectors": [
                            {
                                "id": "CON-1",
                                "standard": "GBT_DC",
                                "format": "CABLE",
                                "power_type": "DC",
                                "max_voltage": 750,
                                "max_amperage": 250,
                                "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                            }
                        ]
                    }
                ],
                "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            }
        ]
    }

@app.get("/ocpi/cpo/2.2.1/tariffs")
async def get_tariffs():
    """Новий ендпоінт: Повертає комерційні тарифи оператора"""
    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": [
            {
                "id": "TAR-001",
                "currency": "UAH",
                "price_components": [
                    {
                        "type": "ENERGY",        # Оплата за спожиту електроенергію
                        "price": 15.00,          # 15 грн за 1 кВт-год
                        "step_size": 1
                    }
                ],
                "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            }
        ]
    }

@app.get("/ocpi/cpo/2.2.1/sessions")
async def get_sessions():
    """Новий ендпоінт: Повертає список активних сесій заряджання"""
    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": [
            {
                "id": "SESS-101",
                "start_date_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "kwh": 24.5,                     # Уже залито 24.5 кВт-год
                "auth_id": "RFID-USER-777",
                "auth_method": "AUTH_REQUEST",
                "location_id": "LOC-001",
                "evse_uid": "EVSE-001",
                "connector_id": "CON-1",
                "currency": "UAH",
                "total_cost": 367.50,            # Поточна вартість (24.5 * 15 грн)
                "status": "ACTIVE",              # Сесія триває прямо зараз
                "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            }
        ]
    }

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
