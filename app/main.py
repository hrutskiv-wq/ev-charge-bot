import asyncio
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

from app.database.connection import init_postgres, close_postgres 
from app.handlers.ocpi_stations import router as ocpi_router
from app.handlers.user import router as user_router
from app.handlers.charge import router as charge_router
from app.api.payments import payments_router
from app.database.ocpi_repo import init_ocpi_tables
# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Ініціалізація бота та диспетчера
bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

# Налаштування Lifespan для FastAPI (керує запуском та зупинкою сервісів)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    # 1. Ініціалізуємо підключення до бази даних
    await init_postgres()
    await init_ocpi_tables()
    # 2. Запускаємо Telegram Bot Polling у фоновому таску
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

# Створюємо FastAPI додаток з прив'язкою до нашого lifespan
app = FastAPI(lifespan=lifespan)

# Зберігаємо бот у state додатка для доступу з ендпоінтів без циклічного імпорту
app.state.bot = bot

# Реєстрація роутерів aiogram (Telegram-апдейти)
dp.include_router(ocpi_router)
dp.include_router(user_router)
dp.include_router(charge_router)

# Реєстрація роутерів FastAPI (HTTP API)
app.include_router(payments_router)

@dp.errors()
async def global_error_handler(event: ErrorEvent, bot: Bot):
    logging.error(f"Global error: {event.exception}")
    return True

# Точка входу для Docker-контейнера
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
