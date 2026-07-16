import asyncio
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from aiogram.types import ErrorEvent

# ЄДИНИЙ спільний bot/dp на весь застосунок (див. app/core/loader.py) —
# раніше тут створювався ще один окремий Bot(token=...)/Dispatcher(),
# що разом із аналогічним у server.py давало три Bot-клієнти з одним
# токеном одночасно (ризик 409 Conflict від Telegram getUpdates).
from app.core.loader import bot, dp

from app.database.connection import init_postgres, close_postgres
from app.services.ocm_service import find_three_nearest_stations
from app.handlers.ocpi_stations import router as bot_stations_router
from app.api.ocpi import router as api_cdr_router
from app.handlers.user import router as user_router
from app.handlers.charge import router as charge_router
from app.api.payments import payments_router
from app.database.ocpi_repo import init_ocpi_tables

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    await init_postgres()
    await init_ocpi_tables()
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
