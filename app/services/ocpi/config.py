import os


class OCPIConfig:
    # Точка підключення веде на сервер оператора (CPO). Для локальної розробки
    # використовується mock_cpo.py на 127.0.0.1:8080.
    CPO_BASE_URL = os.getenv("OCPI_CPO_BASE_URL", "http://127.0.0.1:8080")

    # Спільний секрет для взаємної автентифікації CPO <-> eMSP (OCPI 2.2.1).
    # Немає безпечного дефолту навмисно: якщо токен не заданий у .env,
    # застосунок повинен впасти при старті, а не піднятися з відомим усім
    # тестовим токеном.
    OCPI_TOKEN = os.getenv("OCPI_SECRET_TOKEN")
    if not OCPI_TOKEN:
        raise RuntimeError(
            "❌ OCPI_SECRET_TOKEN не заданий у середовищі! "
            "Це критична змінна для авторизації OCPI-запитів (вхідних і вихідних). "
            "Задайте її у .env перед запуском."
        )

    OCPI_VERSION = "2.2.1"
    PARTY_ID = "EVO"
    COUNTRY_CODE = "UA"
