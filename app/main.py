import os
import logging
import asyncio
import traceback
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

# Імпорти модулів
from app.database.connection import init_postgres, close_postgres
from app.handlers.ocpi_stations import router as ocpi_router
from app.handlers.user import router as user_router
from app.handlers.charge import router as charge_router

# Налаштування логування
logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_postgres()
    yield
    # Shutdown
    await close_postgres()

app = FastAPI(lifespan=lifespan)
bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

# Реєстрація роутерів
dp.include_router(ocpi_router)
dp.include_router(user_router)
dp.include_router(charge_router)

@dp.errors()
async def global_error_handler(event: ErrorEvent, bot: Bot):
    logging.error(f"Global error: {event.exception}")
    return True

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
