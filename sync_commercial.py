import asyncio
import logging

from app.database.connection import get_db_pool
from app.services.ocpi.client import OCPIClient
from app.database.ocpi_repo import init_ocpi_tables, save_ocpi_location, save_ocpi_tariff, save_ocpi_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def run_full_sync():
    print("\n=== FULL COMMERCIAL SYNC ===")
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

    urls = {ep["identifier"]: ep["url"] for ep in details_res["data"]["endpoints"]}

    if "locations" in urls:
        loc_data = await client.get_locations(urls["locations"])
        for loc in (loc_data or {}).get("data", []):
            await save_ocpi_location(loc)

    if "tariffs" in urls:
        tar_data = await client.get_tariffs(urls["tariffs"])
        for tariff in (tar_data or {}).get("data", []):
            await save_ocpi_tariff(tariff)

    if "sessions" in urls:
        sess_data = await client.get_sessions(urls["sessions"])
        for sess in (sess_data or {}).get("data", []):
            await save_ocpi_session(sess)

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        tariffs = await conn.fetch("SELECT id, price, currency FROM ocpi_tariffs")
        print(f"tariffs: {[dict(r) for r in tariffs]}")

        sessions = await conn.fetch("SELECT id, kwh, amount, status FROM ocpi_sessions")
        print(f"sessions: {[dict(r) for r in sessions]}")


if __name__ == "__main__":
    asyncio.run(run_full_sync())
