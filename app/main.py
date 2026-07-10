import os
import logging
import asyncio
import traceback
import html
import uvicorn  # <-- Додали для запуску сервера
from fastapi import FastAPI, Request  # <-- Додали для вебхуків
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

# Інструменти для зв'язку Aiogram FSM з Redis
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio.client import Redis

# Імпорти для PostgreSQL
from app.database.connection import init_postgres, close_postgres

# Імпортуємо роутери
from app.handlers.user import router as user_router
from app.handlers.charge import charge_router

# =====================================================================
# НАЛАШТУВАННЯ FASTAPI ДЛЯ МОНОБАНКУ (ВЕБХУКИ)
# =====================================================================
fastapi_app = FastAPI(title="eVolt UA API")

# 👍 МЕТОД GET: Лікує помилку 405. Потрібен для перевірки посилання Монобанком
@fastapi_app.get("/webhook/monobank")
async def monobank_webhook_verification():
    logging.info("🔍 Отримано перевірочний GET-запит від Monobank або браузера")
    return {"status": "ok"}

# 💰 МЕТОД POST: Сюди прилітатимуть реальні сповіщення про гроші
@fastapi_app.post("/webhook/monobank")
async def monobank_webhook(request: Request):
    try:
        payload = await request.json()
        logging.info(f"💰 ОТРИМАНО ВЕБХУК ВІД MONOBANK: {payload}")
        
        # Отримуємо бота з пам'яті сервера, щоб відправити сповіщення користувачу в майбутньому
        bot: Bot = fastapi_app.state.bot
        
        # TODO: Тут викликатиметься функція обробки платежу (наприклад, з файлу payments.py)
        
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"💥 Помилка обробки вебхуку Monobank: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}


# =====================================================================
# ГЛОБАЛЬНИЙ ОБРОБНИК