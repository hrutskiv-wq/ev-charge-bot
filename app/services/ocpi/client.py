import httpx
import logging
from .config import OCPIConfig

logger = logging.getLogger(__name__)

class OCPIClient:
    def __init__(self):
        self.config = OCPIConfig()
        self.headers = {
            "Authorization": f"Token {self.config.OCPI_TOKEN}",
            "Content-Type": "application/json",
            "X-Request-ID": "12345",
            "X-Correlation-ID": "67890"
        }

    async def get_versions(self):
        async with httpx.AsyncClient() as client:
            try:
                url = f"{self.config.CPO_BASE_URL}/ocpi/versions"
                response = await client.get(url, headers=self.headers, timeout=10.0)
                if response.status_code == 200: return response.json()
                return None
            except Exception as e:
                logger.error(f"Помилка версій: {str(e)}")
                return None

    async def get_version_details(self, version_url: str):
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(version_url, headers=self.headers, timeout=10.0)
                if response.status_code == 200: return response.json()
                return None
            except Exception as e:
                logger.error(f"Помилка деталей: {str(e)}")
                return None

    async def _get_module_data(self, url: str, module_name: str):
        """
        Спільна реалізація GET-запиту до будь-якого OCPI-модуля (locations,
        tariffs, sessions), URL якого віддає get_version_details(). Раніше
        sync_locations.py та sync_commercial.py викликали get_locations(),
        get_tariffs(), get_sessions() — методів, яких у цьому класі взагалі
        не було, тому обидва скрипти падали з AttributeError ще до того,
        як щось встигало записатись у базу.
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=self.headers, timeout=15.0)
                if response.status_code == 200:
                    return response.json()
                logger.error(f"CPO повернув помилку для модуля '{module_name}'. Статус: {response.status_code}")
                return None
            except Exception as e:
                logger.error(f"Мережева помилка отримання модуля '{module_name}': {str(e)}")
                return None

    async def get_locations(self, locations_url: str):
        """Отримує список локацій (OCPI module: locations) з сервера CPO."""
        return await self._get_module_data(locations_url, "locations")

    async def get_tariffs(self, tariffs_url: str):
        """Отримує список тарифів (OCPI module: tariffs) з сервера CPO."""
        return await self._get_module_data(tariffs_url, "tariffs")

    async def get_sessions(self, sessions_url: str):
        """Отримує список сесій (OCPI module: sessions) з сервера CPO."""
        return await self._get_module_data(sessions_url, "sessions")

    async def send_remote_command(self, base_commands_url: str, command: str, payload: dict):
        """Новий метод: Надсилає асинхронну команду START або STOP на сервер оператора"""
        async with httpx.AsyncClient() as client:
            try:
                url = f"{base_commands_url}/{command.upper()}"
                logger.info(f"📡 Відправка OCPI команди [{command.upper()}] на ендпоінт: {url}")

                response = await client.post(url, headers=self.headers, json=payload, timeout=10.0)
                if response.status_code == 200:
                    return response.json()
                logger.error(f"Оператор повернув помилку виконання команди. Статус: {response.status_code}")
                return None
            except Exception as e:
                logger.error(f"Мережева помилка відправки команди OCPI: {str(e)}")
                return None
