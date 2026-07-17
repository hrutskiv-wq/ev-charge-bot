import httpx
import asyncio
import os
from dotenv import load_dotenv

from app.database.connection import init_postgres, close_postgres, save_station_to_local_db

load_dotenv()
OCM_KEY = os.getenv("OCM_KEY")


async def sync_ukraine_stations():
    API_URL = "https://api.openchargemap.io/v3/poi/"

    params = {
        "output": "json",
        "countrycode": "UA",
        "maxresults": 100,
        "key": OCM_KEY
    }

    print("loading stations from OCM...")

    await init_postgres()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL, params=params)
            if response.status_code != 200:
                print(f"OCM error status: {response.status_code}")
                return

            stations_data = response.json()
            count = 0
            for poi in stations_data:
                station_id = f"OCM-{poi['ID']}"

                addr_info = poi.get('AddressInfo', {})
                name = addr_info.get('Title', 'station')
                address = addr_info.get('AddressLine1', 'address')
                lat = addr_info.get('Latitude')
                lon = addr_info.get('Longitude')
                operator_info = poi.get('OperatorInfo') or {}
                operator = operator_info.get('Title', 'unknown')

                if lat and lon:
                    await save_station_to_local_db(
                        station_id=station_id,
                        name=name,
                        address=address,
                        connectors="",
                        lat=lat,
                        lon=lon,
                        operator=operator,
                    )
                    count += 1

            print(f"synced {count} stations into postgres")

    except Exception as e:
        print(f"error: {e}")
    finally:
        await close_postgres()


if __name__ == "__main__":
    asyncio.run(sync_ukraine_stations())
