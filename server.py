"""
Сумісний шім для `uvicorn server:app`.

Раніше цей файл був повноцінним, але окремим від app/main.py входом у
застосунок: власний Bot()/Dispatcher(), власний lifespan, і що найгірше —
імпортував з app.database.connection функції initialize_db,
find_three_nearest_stations та об'єкт db_connection, яких там не існує.
Через це `uvicorn server:app` (саме так налаштований evolt_bot.service)
падав з ImportError одразу при старті — systemd-сервіс фактично не міг
піднятись.

Єдине реальне джерело правди — app/main.py (там і Bot/Dispatcher з
app.core.loader, і PWA-ендпоінти, і CORS, і lifespan з ініціалізацією БД).
Цей файл лишається лише для зворотної сумісності зі старими скриптами
запуску; краще оновити їх на `app.main:app` напряму (див. evolt_bot.service).
"""

from app.main import app  # noqa: F401
