import os
import logging
import asyncio
import traceback
import html
import uvicorn
<<<<<<< HEAD
from contextlib import asynccontextmanager  # <-- Керування життєвим циклом
=======
from contextlib import asynccontextmanager
>>>>>>> d26a9bd (feat: повна синхронізація хоста та контейнера для OCPI модулів)
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio.client import Redis
from app.database.connection import init_postgres, close_postgres

from app.handlers.ocpi_stations import router as ocpi_router
from app.handlers.user import router as user_router
from app.handlers.charge import charge_router
from app.handlers.ocpi_stations import router as ocpi_router  # <-- ДОДАЛИ НАШ OCPI РОУТЕР

async def global_error_handler(event: ErrorEvent, bot: Bot):
    exception = event.exception
    update = event.update
    if "message is not modified" in str(exception):
        try:
            if update.callback_query:
                await update.callback_query.answer()
        except Exception:
            pass
        return
    logging.error(f"💥 Критична помилка: {exception}", exc_info=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    await init_postgres()
    bot_token = os.getenv("BOT_TOKEN")
    bot = Bot(token=bot_token)
    app.state.bot = bot
    redis_host = os.getenv("REDIS_HOST", "redis" if os.path.exists("/.dockerenv") else "localhost")
    redis_client = Redis(host=redis_host, port=6379, decode_responses=True)
    storage = RedisStorage(redis=redis_client)
    dp = Dispatcher(storage=storage)
<<<<<<< HEAD

   # Реєстрація обробників та роутерів
    dp.errors.register(global_error_handler)
    dp.include_router(ocpi_router)   # <-- ТЕПЕР ВІН ПЕРШИЙ І ПЕРЕХОПЛЮЄ КОМАНДУ ОДРАЗУ!
=======
    dp.errors.register(global_error_handler)

    # Наш роутер OCPI перехоплює команду першим
    dp.include_router(ocpi_router)
>>>>>>> d26a9bd (feat: повна синхронізація хоста та контейнера для OCPI модулів)
    dp.include_router(charge_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    polling_task = asyncio.create_task(dp.start_polling(bot))
    yield
    polling_task.cancel()
    await close_postgres()

<<<<<<< HEAD

# Инициализируем FastAPI с привязкой к нашему жизненному циклу
=======
>>>>>>> d26a9bd (feat: повна синхронізація хоста та контейнера для OCPI модулів)
fastapi_app = FastAPI(title="eVolt UA API", lifespan=lifespan)

@fastapi_app.post("/webhook/monobank")
async def monobank_webhook(request: Request):
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
