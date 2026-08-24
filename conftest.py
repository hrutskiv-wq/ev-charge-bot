import os

# OCPIConfig (app/services/ocpi/config.py) навмисно кидає RuntimeError при
# імпорті, якщо OCPI_SECRET_TOKEN не заданий у середовищі (це один із самих
# фіксів, який ми тестуємо). Тому змінну треба виставити ДО того, як
# pytest почне імпортувати тестові модулі, що тягнуть за собою app.api.ocpi
# -> app.services.ocpi.config. conftest.py гарантовано завантажується
# раніше за сусідні test_*.py файли в тій самій теці.
os.environ.setdefault("OCPI_SECRET_TOKEN", "test-ocpi-token-for-pytest")

# Те саме, але з іншої причини: ці дві змінні валять не імпорт app.api.ocpi,
# а створення єдиного Bot/Dispatcher в app/core/loader.py — BOT_TOKEN дає
# aiogram.utils.token.TokenValidationError у конструкторі Bot(), а
# GEMINI_API_KEY — genai.Client(api_key=None). Оскільки loader тягнеться
# майже з кожного хендлера, без них pytest падав ще на ЗБИРАННІ: 11 файлів
# не імпортувались, тобто сюїта з чистого термінала не запускалась узагалі,
# і кожна нова сесія відкривала це заново.
#
# setdefault, а не привласнення: справжнє значення з середовища завжди
# перемагає. У CI змінні приходять з ci.yml і ці рядки нічого не роблять.
#
# Значення — ті самі плейсхолдери, що в .github/workflows/ci.yml (job
# `test` і `live-db-tests`), навмисно дослівно: якщо тест колись почне
# залежати від конкретного значення, локальний прогін і CI мають розійтись
# не мовчки. Формат BOT_TOKEN саме такий, бо aiogram валідує його як
# "<digits>:<string>" ще до будь-якого мережевого виклику.
os.environ.setdefault("BOT_TOKEN", "123456:ci-placeholder-bot-token")
os.environ.setdefault("GEMINI_API_KEY", "ci-placeholder-gemini-key")
