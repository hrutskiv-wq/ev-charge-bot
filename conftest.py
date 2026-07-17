import os

# OCPIConfig (app/services/ocpi/config.py) навмисно кидає RuntimeError при
# імпорті, якщо OCPI_SECRET_TOKEN не заданий у середовищі (це один із самих
# фіксів, який ми тестуємо). Тому змінну треба виставити ДО того, як
# pytest почне імпортувати тестові модулі, що тягнуть за собою app.api.ocpi
# -> app.services.ocpi.config. conftest.py гарантовано завантажується
# раніше за сусідні test_*.py файли в тій самій теці.
os.environ.setdefault("OCPI_SECRET_TOKEN", "test-ocpi-token-for-pytest")
