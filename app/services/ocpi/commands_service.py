import logging
import uuid
import os
from app.database import connection
from app.services.ocpi.client import OCPIClient

logger = logging.getLogger(__name__)

class OCPICommandsService:
    def __init__(self):
        self.client = OCPIClient()
        # Зчитуємо адресу нашого сервера з .env
        self.emsp_base_url = os.getenv("EMSP_BASE_URL", "https://evolt.ua").rstrip("/")

    async def initiate_start_session(self, user_id: int, location_id: str, evse_uid: str, connector_id: str, base_commands_url: str) -> dict:
        """
        Високорівневий метод для перевірки умов та запуску зарядки через OCPI.
        """
        # 1. Фінансова безпека: перевіряємо поточний баланс користувача.
        #    Читаємо users.balance (через get_user_data) — це те саме кешоване
        #    значення, яке показується користувачу в боті, і воно завжди
        #    оновлюється атомарно разом із kw_transactions в update_user_balance().
        #    Раніше тут рахувалась SUM(kw_transactions.amount) окремим запитом —
        #    інше джерело правди, ніж те, що бачив користувач в /start.
        balance, _ = await connection.get_user_data(user_id)

        # Якщо баланс нульовий або мінусовий — блокуємо запуск
        if balance <= 0:
            logger.warning(f"Користувач {user_id} намагався запустити зарядку з нульовим балансом ({balance} кВт·год)")
            return {"status": "REJECTED", "message": "❌ Недостатньо кВт·год на балансі для старту зарядки."}

        # 2. Генеруємо унікальний токен авторизації для цієї сесії (вимоги OCPI 2.2.1)
        session_token = f"EVOLT-{user_id}-{uuid.uuid4().hex[:6].upper()}"

        # 3. Формуємо стандартний payload згідно специфікації OCPI 2.2.1
        payload = {
            "response_url": f"{self.emsp_base_url}/ocpi/emsp/2.2.1/callback/commands/START_SESSION/{user_id}",
            "token": {
                "uid": session_token,
                "type": "APP_USER",
                "auth_id": str(user_id),
                "visual_number": f"EVOLT-{user_id}",
                "issuer": "eVolt UA",
                "valid": True
            },
            "location_id": location_id,
            "evse_uid": evse_uid,
            "connector_id": connector_id
        }

        # 4. Викликаємо твій метод з OCPIClient
        result = await self.client.send_remote_command(
            base_commands_url=base_commands_url,
            command="START_SESSION",
            payload=payload
        )

        if result and result.get("statusCode") == 1000:
            logger.info(f"CPO успішно прийняв команду старту для користувача {user_id}. Токен: {session_token}")
            return {"status": "ACCEPTED", "message": "⚡ Запит на запуск надіслано на станцію. Очікуйте блокування кабелю та увімкнення..."}
            
        return {"status": "FAILED", "message": "❌ Станція відхилила запит на запуск. Спробуйте ще раз або змініть конектор."}

    async def initiate_stop_session(self, user_id: int, session_id: str, base_commands_url: str) -> dict:
        """
        Метод для зупинки активної сесії заряджання через OCPI.
        """
        # Формуємо payload для STOP_SESSION згідно зі специфікацією OCPI 2.2.1
        payload = {
            "response_url": f"{self.emsp_base_url}/ocpi/emsp/2.2.1/callback/commands/STOP_SESSION/{user_id}",
            "session_id": session_id
        }

        # Викликаємо твій метод з OCPIClient для відправки STOP_SESSION
        result = await self.client.send_remote_command(
            base_commands_url=base_commands_url,
            command="STOP_SESSION",
            payload=payload
        )

        if result and result.get("statusCode") == 1000:
            logger.info(f"CPO успішно прийняв команду зупинки для сесії {session_id} (користувач {user_id}).")
            return {"status": "ACCEPTED", "message": "🛑 Запит на зупинку надіслано. Очікуйте завершення сесії..."}
            
        return {"status": "FAILED", "message": "❌ Не вдалося надіслати команду зупинки. Будь ласка, спробуйте знову."}
