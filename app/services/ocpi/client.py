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
        """Крок 1: Запит підтримуваних версій"""
        async with httpx.AsyncClient() as client:
            try:
                url = f"{self.config.CPO_BASE_URL}/ocpi/versions"
                logger.info(f"Запит версій OCPI з: {url}")
                response = await client.get(url, headers=self.headers, timeout=10.0)
                if response.status_code == 200:
                    return response.json()
                logger.error(f"Помилка отримання версій. Статус: {response.status_code}")
                return None
            except Exception as e:
                logger.error(f"Помилка мережі при запиті версій: {str(e)}")
                return None

    async def get_version_details(self, version_url: str):
        """Крок 2: Запит деталей версії (списку модулів)"""
        async with httpx.AsyncClient() as client:
            try:
                logger.info(f"Запит модулів версії OCPI з: {version_url}")
                response = await client.get(version_url, headers=self.headers, timeout=10.0)
                if response.status_code == 200:
                    return response.json()
                logger.error(f"Помилка отримання деталей версії. Статус: {response.status_code}")
                return None
            except Exception as e:
                logger.error(f"Помилка мережі при запиті деталей версії: {str(e)}")
                return None
