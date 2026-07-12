import os

class OCPIConfig:
    # Точка підключення тепер веде на наш локальний mock-сервер
    CPO_BASE_URL = os.getenv("OCPI_CPO_BASE_URL", "http://127.0.0.1:8080")
    
    OCPI_TOKEN = os.getenv("OCPI_SECRET_TOKEN", "local_test_token_123")
    OCPI_VERSION = "2.2.1"
    PARTY_ID = "EVO"
    COUNTRY_CODE = "UA"
