import asyncio
import logging
from app.services.ocpi.client import OCPIClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def test_remote_commands():
    print("\n=== 🎮 ЗАПУСК ТЕСТУ ДИСТАНЦІЙНОГО КЕРУВАННЯ СТАНЦІЄЮ (OCPI COMMANDS) 🎮 ===")
    client = OCPIClient()
    
    # 1. Проходимо швидкий handshake для динамічного пошуку ендпоінту команд
    versions = await client.get_versions()
    version_url = versions["data"][0]["url"]
    details = await client.get_version_details(version_url)
    
    commands_base_url = None
    for ep in details["data"]["endpoints"]:
        if ep["identifier"] == "commands":
            commands_base_url = ep["url"]
            
    if not commands_base_url:
        print("❌ На сервері не активовано модуль commands.")
        return

    # 2. Формуємо бізнес-payload для старту зарядки
    start_payload = {
        "response_url": "https://evolt-bot.com/ocpi/callback/commands/123", # Куди оператор пришле фінальний статус
        "token": {
            "uid": "RFID-USER-777",
            "type": "RFID",
            "auth_id": "EVO-USER-001",
            "visual_number": "112233",
            "issuer": "EVO",
            "whitelist": "ALLOWED"
        },
        "location_id": "LOC-001",
        "evse_uid": "EVSE-001",
        "connector_id": "CON-1"
    }

    # 3. Надсилаємо команду START_SESSION
    result = await client.send_remote_command(commands_base_url, "START_SESSION", start_payload)
    
    print("\n=== 🎯 РЕЗУЛЬТАТ ВИКОНАННЯ КОМАНДИ ОПЕРАТОРОМ ===")
    if result and result.get("status_code") == 1000:
        cmd_status = result["data"]["result"]
        print(f"Статус відповіді заліза: [{cmd_status}]")
        if cmd_status == "ACCEPTED":
            print("🚀 Успіх! Станція прийняла команду від нашого бота та замикає реле зарядки.")
    else:
        print("❌ Станція відхилила команду дистанційного запуску.")
    print("========================================================================\n")

if __name__ == "__main__":
    asyncio.run(test_remote_commands())
