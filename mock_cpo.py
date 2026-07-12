from fastapi import FastAPI
from datetime import datetime, timezone
import uvicorn

app = FastAPI(title="OCPI CPO Mock Server")

@app.get("/ocpi/versions")
async def get_versions():
    """Крок 1: Список підтримуваних версій"""
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
    """Крок 2: Список доступних модулів для версії 2.2.1"""
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
                    "identifier": "sessions",
                    "role": "SENDER",
                    "url": "http://127.0.0.1:8080/ocpi/cpo/2.2.1/sessions"
                },
                {
                    "identifier": "tariffs",
                    "role": "SENDER",
                    "url": "http://127.0.0.1:8080/ocpi/cpo/2.2.1/tariffs"
                }
            ]
        }
    }

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
