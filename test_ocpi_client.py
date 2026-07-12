import asyncio
import logging
from app.services.ocpi.client import OCPIClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def test_full_handshake():
    print("\n=== ⚡ ЗАПУСК ПОВНОГО ТЕСТУ OCPI HANDSHAKE ⚡ ===")
    client = OCPIClient()
    
    # 1. Отримуємо версії
    versions_res = await client.get_versions()
    if not versions_res or "data" not in versions_res:
        print("❌ Крок 1 провалено.")
        return
        
    print("✅ Крок 1 успішний! Отримано список версій.")
    
    # Витягуємо URL для версії 2.2.1
    version_221_url = None
    for v in versions_res["data"]:
        if v["version"] == "2.2.1":
            version_221_url = v["url"]
            
    if not version_221_url:
        print("❌ У відповіді сервера не знайдено версію 2.2.1.")
        return
        
    # 2. Отримуємо модулі за цим посиланням
    print(f"Автоматично переходимо до Кроку 2. Посилання: {version_221_url}")
    details_res = await client.get_version_details(version_221_url)
    
    print("\n=== 🎯 ФІНАЛЬНИЙ РЕЗУЛЬТАТ (ДОСТУПНІ МОДУЛІ) ===")
    if details_res and details_res.get("status_code") == 1000:
        print("Успішне наскрізне рукостискання!")
        endpoints = details_res["data"]["endpoints"]
        for ep in endpoints:
            print(f"📦 Модуль: [{ep['identifier']}] | Роль: {ep['role']} | URL: {ep['url']}")
    else:
        print("❌ Не вдалося отримати модулі версії.")
    print("==================================================\n")

if __name__ == "__main__":
    asyncio.run(test_full_handshake())
