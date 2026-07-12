import os
import logging
import asyncio
import traceback
import html
import uvicorn
from contextlib import asynccontextmanager  # <-- Керування життєвим циклом
from fastapi import FastAPI, Request
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
from app.handlers.ocpi_stations import router as ocpi_router  # <-- ДОДАЛИ НАШ OCPI РОУТЕР

# =====================================================================
# ГЛОБАЛЬНИЙ ОБРОБНИК ПОМИЛОК ТЕЛЕГРАМ
# =====================================================================
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
    
    tb_lines = traceback.format_exception(type(exception), exception, exception.__traceback__)
    tb_text = "".join(tb_lines)
    
    if len(tb_text) > 3500:
        tb_text = tb_text[-3500:]
        
    logs_chat_id = os.getenv("LOGS_CHAT_ID")
    
    if logs_chat_id:
        safe_exception = html.escape(str(exception))
        safe_tb_text = html.escape(tb_text)

        error_message = (
            f"🚨 <b>Критичний збій у системі eVolt UA!</b>\n\n"
            f"🪲 <b>Помилка:</b> <code>{safe_exception}</code>\n\n"
            f"📋 <b>Детальний Traceback:</b>\n"
            f"<pre><code class='language-python'>{safe_tb_text}</code></pre>"
        )
        try:
            await bot.send_message(chat_id=logs_chat_id, text=error_message, parse_mode="HTML")
        except Exception as log_err:
            logging.error(f"Не вдалося надіслати лог у Telegram-чат: {log_err}")

    try:
        if update.message:
            await update.message.answer(
                "⚠️ <b>Вибачте, виникла тимчасова технічна помилка.</b>\n"
                "Наші інженери вже отримали звіт і виправляють її. Спробуйте, будь ласка, за хвилину!",
                parse_mode="HTML"
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "⚠️ Технічний збій. Інженери вже сповіщені.", show_alert=True
            )
    except Exception as reply_err:
        logging.error(f"Не вдалося відповісти користувачу після помилки: {reply_err}")


# =====================================================================
# КЕРУВАННЯ ЗАПУСКОМ ЧЕРЕЗ LIFESPAN
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 💥 СТАРТ СИСТЕМИ (Виконується, коли запускається Docker контейнер)
    logging.basicConfig(level=logging.INFO)
    logging.info("🎬 Ініціалізація системи eVolt UA...")

    # 1. Підключаємо PostgreSQL
    await init_postgres()

    # 2. Налаштовуємо бота
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN не знайдено в змінних оточення!")
    bot = Bot(token=bot_token)
    app.state.bot = bot  # Зберігаємо у FastAPI

    # 3. Налаштовуємо Redis та Диспетчер
    redis_host = os.getenv("REDIS_HOST", "redis" if os.path.exists("/.dockerenv") else "localhost")
    redis_client = Redis(host=redis_host, port=6379, decode_responses=True)
    storage = RedisStorage(redis=redis_client)
    dp = Dispatcher(storage=storage)

   # Реєстрація обробників та роутерів
    dp.errors.register(global_error_handler)
    dp.include_router(ocpi_router)   # <-- ТЕПЕР ВІН ПЕРШИЙ І ПЕРЕХОПЛЮЄ КОМАНДУ ОДРАЗУ!
    dp.include_router(charge_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    
    # 4. Запускаємо Телеграм-поллінг як фонову задачу в єдиному циклі подій
    polling_task = asyncio.create_task(dp.start_polling(bot))
    logging.info("🚀 Бот та вебсервер FastAPI успішно запущені в одному циклі!")

    yield  # <--- Тут додаток живе, сервер працює і приймає запити від Монобанку

    # 🔒 ЗУПИНКА СИСТЕМИ (Виконується, коли пишемо docker compose down)
    logging.info("🛑 Зупинка системи eVolt UA...")
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    
    await close_postgres()
    logging.info("💤 Система чисто закрила всі підключення.")


# Инициализируем FastAPI с привязкой к нашему жизненному циклу
fastapi_app = FastAPI(title="eVolt UA API", lifespan=lifespan)


# =====================================================================
# МАРШРУТИ ДЛЯ ВЕБХУКІВ MONOBANK
# =====================================================================
@fastapi_app.get("/webhook/monobank")
async def monobank_webhook_verification():
    logging.info("🔍 Отримано перевірочний GET-запит від Monobank або браузера")
    return {"status": "ok"}

@fastapi_app.post("/webhook/monobank")
async def monobank_webhook(request: Request):
    try:
        payload = await request.json()
        logging.info(f"💰 ОТРИМАНО ВЕБХУК ВІД MONOBANK: {payload}")
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"💥 Помилка обробки вебхуку Monobank: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}


if __name__ == "__main__":
    # Запускаємо uvicorn як головний процес
    uvicorn.run(fastapi_app, host="0.0.0.0", port=8000, log_level="info")