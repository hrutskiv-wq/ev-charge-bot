import asyncio
import logging
from app.services.ocpi.client import OCPIClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def test_full_commercial_flow():
    print("\n=== ⚡ ЗАПУСК МАСШТАБНОГО ТЕСТУ КОМЕРЦІЙНИХ ЕНДПОІНТІВ OCPI ⚡ ===")
    client = OCPIClient()
    
    # 1. Рукостискання
    versions_res = await client.get_versions()
    if not versions_res: return
    version_221_url = versions_res["data"][0]["url"]
    
    details_res = await client.get_version_details(version_221_url)
    if not details_res: return
    
    # Витягуємо URL-адреси для кожного активованого модуля
    urls = {}
    for ep in details_res["data"]["endpoints"]:
        urls[ep["identifier"]] = ep["url"]
        
    # 2. Запит Локацій
    if "locations" in urls:
        loc_data = await client.get_locations(urls["locations"])
        print(f"✅ Локації успішно зчитано. Знайдено станцій: {len(loc_data['data'])}")

    # 3. Запит Тарифів
    if "tariffs" in urls:
        tar_data = await client.get_tariffs(urls["tariffs"])
        print("\n=== 🎯 ОБРОБЛЕНІ КОМЕРЦІЙНІ ТАРИФИ ===")
        if tar_data and tar_data.get("status_code") == 1000:
            for tariff in tar_data["data"]:
                print(f"💳 Тариф ID: {tariff['id']}")
                for comp in tariff["price_components"]:
                    print(f"   Тип оплати: {comp['type']} | Вартість: {comp['price']} {tariff['currency']} за 1 кВт-год")
        else:
            print("❌ Не вдалося отримати тарифи.")

    # 4. Запит Активних Сесій
    if "sessions" in urls:
        sess_data = await client.get_sessions(urls["sessions"])
        print("\n=== 🎯 МОНІТОР АКТИВНИХ СЕСІЙ ЗАРЯДЖАННЯ ===")
        if sess_data and sess_data.get("status_code") == 1000:
            for sess in sess_data["data"]:
                print(f"🔋 Сесія ID: {sess['id']} | Статус: {sess['status']}")
                print(f"   Станція ID: {sess['location_id']} | Конектор: {sess['connector_id']}")
                print(f"   Спожито енергії: {sess['kwh']} кВт-год")
                print(f"   Поточний рахунок користувача: {sess['total_cost']} {sess['currency']}")
        else:
            print("❌ Не вдалося отримати сесії.")
    print("=================================================================\n")

if __name__ == "__main__":
    asyncio.run(test_full_commercial_flow())
