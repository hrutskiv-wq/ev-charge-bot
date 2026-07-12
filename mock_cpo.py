from fastapi import FastAPI, Body
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
                    "identifier": "commands",
                    "role": "RECEIVER",
                    "url": "http://127.0.0.1:8080/ocpi/cpo/2.2.1/commands"
                }
            ]
        }
    }

@app.post("/ocpi/cpo/2.2.1/commands/{command_name}")
async def handle_ocpi_command(command_name: str, payload: dict = Body(...)):
    """Ендпоінт для прийому команд START_SESSION та STOP_SESSION від нашого бота"""
    print(f"\n[MOCK CPO] Отримано команду через OCPI: {command_name.upper()}")
    print(f"[MOCK CPO] Дані запиту: {payload}\n")
    
    # За стандартом OCPI, якщо команда валідна, сервер повертає статус ACCEPTED
    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": {
            "result": "ACCEPTED",  # Стане REJECTED, якщо станція зайнята чи офлайн
            "timeout": 30
        }
    }

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
