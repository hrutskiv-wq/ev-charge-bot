import asyncio
import logging

from app.database.connection import get_db_pool
from app.services.ocpi.client import OCPIClient
from app.database.ocpi_repo import init_ocpi_tables, save_ocpi_location

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def run_sync():
    print("\n=== RUN SYNC OCPI -> DATABASE ===")

    await init_ocpi_tables()

    client = OCPIClient()

    versions_res = await client.get_versions()
    if not versions_res:
        print("no versions")
        return

    version_221_url = versions_res["data"][0]["url"]
    details_res = await client.get_version_details(version_221_url)
    if not details_res:
        print("no details")
        return

    locations_url = None
    for ep in details_res["data"]["endpoints"]:
        if ep["identifier"] == "locations":
            locations_url = ep["url"]

    if not locations_url:
        print("no locations endpoint")
        return

    locations_data = await client.get_locations(locations_url)
    if not locations_data or "data" not in locations_data:
        print("no data")
        return

    saved = 0
    for loc in locations_data["data"]:
        await save_ocpi_location(loc)
        saved += 1

    print(f"synced locations: {saved}")

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        locs = await conn.fetch("SELECT id, name, city FROM ocpi_locations")
        print(f"locations in db: {[dict(r) for r in locs]}")

        evses = await conn.fetch("SELECT uid, status FROM ocpi_evses")
        print(f"evses in db: {[dict(r) for r in evses]}")

        connectors = await conn.fetch("SELECT id, standard, power_type FROM ocpi_connectors")
        print(f"connectors in db: {[dict(r) for r in connectors]}")


if __name__ == "__main__":
    asyncio.run(run_sync())
