import httpx
import logging
from .config import OCPIConfig

logger = logging.getLogger(__name__)

class OCPIClient:
    def __init__(self):
        self.config = OCPIConfig()
        # Заголовки, які є обов'язковими за стандартом OCPI 2.2.1
        self.headers = {
            "Authorization": f"Token {self.config.OCPI_TOKEN}",
            "Content-Type": "application/json",
            "X-Request-ID": "12345", # У реальному коді тут буде унікальний uuid
            "X-Correlation-ID": "67890"
        }

    async def get_versions(self):
        """
        Перший крок OCPI рукостискання (Handshake).
        Запитуємо у платформи партнера список підтримуваних версій протоколу.
        """
        async with httpx.AsyncClient() as client:
            try:
                url = f"{self.config.CPO_BASE_URL}/ocpi/versions"
                logger.info(f"Запит версій OCPI з: {url}")
                
                response = await client.get(url, headers=self.headers, timeout=10.0)
                
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"Помилка OCPI Handshake. Статус: {response.status_code}, Відповідь: {response.text}")
                    return None
            except Exception as e:
                logger.error(f"Не вдалося з'єднатися з OCPI CPO сервером: {str(e)}")
                return None
