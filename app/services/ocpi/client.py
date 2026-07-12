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
                if response.status_code == 200:
                    return response.json()
                return None
            except Exception as e:
                logger.error(f"Помилка при запиті версій: {str(e)}")
                return None

    async def get_version_details(self, version_url: str):
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(version_url, headers=self.headers, timeout=10.0)
                if response.status_code == 200:
                    return response.json()
                return None
            except Exception as e:
                logger.error(f"Помилка при запиті деталей версії: {str(e)}")
                return None

    async def get_locations(self, locations_url: str):
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(locations_url, headers=self.headers, timeout=10.0)
                if response.status_code == 200:
                    return response.json()
                return None
            except Exception as e:
                logger.error(f"Помилка при отриманні локацій: {str(e)}")
                return None

    async def get_tariffs(self, tariffs_url: str):
        """Запитує комерційні тарифи оператора"""
        async with httpx.AsyncClient() as client:
            try:
                logger.info(f"💳 Отримання тарифів OCPI з: {tariffs_url}")
                response = await client.get(tariffs_url, headers=self.headers, timeout=10.0)
                if response.status_code == 200:
                    return response.json()
                logger.error(f"Не вдалося отримати тарифи. Статус: {response.status_code}")
                return None
            except Exception as e:
                logger.error(f"Мережева помилка при отриманні тарифів: {str(e)}")
                return None

    async def get_sessions(self, sessions_url: str):
        """Запитує список поточних сесій заряджання"""
        async with httpx.AsyncClient() as client:
            try:
                logger.info(f"🔋 Отримання активних сесій OCPI з: {sessions_url}")
                response = await client.get(sessions_url, headers=self.headers, timeout=10.0)
                if response.status_code == 200:
                    return response.json()
                logger.error(f"Не вдалося отримати сесії. Статус: {response.status_code}")
                return None
            except Exception as e:
                logger.error(f"Мережева помилка при отриманні сесій: {str(e)}")
                return None
