import asyncio
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from aiogram.types import ErrorEvent, BotCommand, MenuButtonCommands

# ЄДИНИЙ спільний bot/dp на весь застосунок (див. app/core/loader.py) —
# раніше тут створювався ще один окремий Bot(token=...)/Dispatcher(),
# що разом із аналогічним у server.py давало три Bot-клієнти з одним
# токеном одночасно (ризик 409 Conflict від Telegram getUpdates).
from app.core.loader import bot, dp

from app.database import connection
from app.database.connection import init_postgres, close_postgres
from app.services.ocm_service import find_three_nearest_stations
from app.handlers.ocpi_stations import router as bot_stations_router
from app.api.ocpi import router as api_cdr_router
from app.handlers.user import router as user_router
from app.handlers.charge import router as charge_router
from app.api.payments import payments_router
from app.database.ocpi_repo import init_ocpi_tables
from app.database.operators_repo import init_operator_tables

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    await init_postgres()
    await init_ocpi_tables()
    await init_operator_tables()

    # Кнопка "Menu" біля поля вводу в Telegram (як у конкурентних ботів) —
    # раніше bot.set_my_commands()/set_chat_menu_button() взагалі не
    # викликались, тому в клієнті був лише стандартний значок клавіатури,
    # без списку команд. MenuButtonCommands() показує саме цей список при
    # натисканні "Menu". Команди дублюють уже наявні кнопки reply-клавіатури
    # (app/keyboards/reply.py) — див. відповідні Command(...) хендлери в
    # app/handlers/user.py.
    await bot.set_my_commands([
        BotCommand(command="start", description="Головне меню"),
        BotCommand(command="balance", description="💳 Баланс і історія операцій"),
        BotCommand(command="charge", description="⚡ Почати зарядку"),
        BotCommand(command="voucher", description="🧾 Поповнити баланс"),
        BotCommand(command="support", description="📢 Online підтримка"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    polling_task = asyncio.create_task(dp.start_polling(bot))
    logging.info("🚀 Telegram bot polling успішно запущено у фоні!")

    yield  # Тут працює наш веб-сервер FastAPI

    # --- SHUTDOWN ---
    logging.info("🛑 Зупинка сервісів...")
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass

    await bot.session.close()
    await dp.storage.close()  # закриває з'єднання з Redis (якщо FSM на RedisStorage)
    await close_postgres()
    logging.info("💤 Всі сервіси безпечно зупинено.")


app = FastAPI(title="eVolt UA API Server", lifespan=lifespan)

# Зберігаємо бот у state додатка для доступу з ендпоінтів без циклічного імпорту
app.state.bot = bot

# CORS: дозволяємо лише явно перелічені джерела (env PWA_ALLOWED_ORIGINS,
# через кому, напр. "https://evolt.ua,https://app.evolt.ua"). Раніше було
# allow_origins=["*"] разом з allow_credentials=True в server.py — невалідна
# (браузери таку комбінацію відхиляють) і небезпечна конфігурація.
_allowed_origins = [
    origin.strip()
    for origin in os.getenv("PWA_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Реєстрація роутерів FastAPI (HTTP API)
app.include_router(api_cdr_router)
app.include_router(payments_router)

# Реєстрація роутерів aiogram (Telegram-апдейти)
dp.include_router(bot_stations_router)
dp.include_router(user_router)
dp.include_router(charge_router)


@dp.errors()
async def global_error_handler(event: ErrorEvent, bot):
    logging.error(f"Global error: {event.exception}")
    return True


@app.get("/health")
async def health_check():
    """
    Перевірка живучості для docker-compose (`condition: service_healthy`) і
    зовнішнього моніторингу аптайму. Раніше такого ендпоінту не було взагалі
    — контейнер вважався "здоровим", якщо просто відповідав на HTTP, навіть
    якщо реально відвалилось з'єднання з Postgres (бот при цьому міг далі
    приймати запити й падати на кожному, що торкається БД).
    """
    checks = {}
    healthy = True

    try:
        if connection.db_pool is None:
            raise RuntimeError("пул підключень до PostgreSQL ще не ініціалізовано")
        async with connection.db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"
        healthy = False

    # Redis перевіряємо, лише якщо FSM реально налаштований на RedisStorage
    # (див. app/core/loader.py) — при MemoryStorage (локальна розробка без
    # Redis) відсутність Redis не є ознакою нездорового застосунку.
    redis_client = getattr(dp.storage, "redis", None)
    if redis_client is not None:
        try:
            await redis_client.ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"error: {e}"
            healthy = False
    else:
        checks["redis"] = "not configured (MemoryStorage)"

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


# --- PWA / веб-інтерфейс (раніше жило окремо у дублюючому server.py) ---

@app.get("/api/stations")
async def get_stations(lat: float = Query(...), lon: float = Query(...)):
    stations = await find_three_nearest_stations(lat, lon)
    if not stations:
        return {"success": False, "stations": []}
    return {"success": True, "stations": stations}


@app.get("/pwa")
async def get_pwa_index():
    return FileResponse("public/index.html")


# Підключаємо статичні файли (маніфест, іконки, сервіс-воркер).
# Монтується останнім, щоб не перекривати API-роути вище.
app.mount("/", StaticFiles(directory="public"), name="public")

# Точка входу для Docker-контейнера
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
