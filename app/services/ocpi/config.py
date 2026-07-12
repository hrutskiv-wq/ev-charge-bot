import os

class OCPIConfig:
    # URL тестового або реального сервера оператора (CPO)
    CPO_BASE_URL = os.getenv("OCPI_CPO_BASE_URL", "https://sandbox.cpo-partner.com")
    
    # Токен безпеки (Token B), який видасть оператор для авторизації запитів
    OCPI_TOKEN = os.getenv("OCPI_SECRET_TOKEN", "your_test_token_here")
    
    # Версія протоколу, яку використовує оператор (зазвичай 2.2.1)
    OCPI_VERSION = "2.2.1"
    
    # Назва нашої системи для ідентифікації в логах оператора
    PARTY_ID = "EVO"
    COUNTRY_CODE = "UA"
