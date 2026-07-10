import os
import logging
import asyncio
import traceback
import html
import uvicorn  # <--- Додали для запуску сервера
from fastapi import FastAPI, Request  # <--- Додали для вебхуків
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
# НАЛАШТУВАННЯ FASTAPI ДЛЯ МОНОБАНКУ
# =====================================================================
fastapi_app = FastAPI(title="eVolt UA API")

@fastapi_app.post("/webhook/monobank")
async def monobank_webhook(request: Request):
    try:
        payload = await request.json()
        logging.info(f"💰 ОТРИМАНО ВЕБХУК ВІД MONOBANK: {payload}")
        
        # Отримуємо бота з глобального стану сервера, щоб відправити сповіщення користувачу
        bot: Bot = fastapi_app.state.bot
        
        # TODO: Тут ти викликатимеш функцію обробки платежу зі свого файлу payments.py
        # Наприклад: await process_payment(payload, bot)
        
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"💥 Помилка обробки вебхуку Monobank: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}

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
                "⚠️ <b>Вибачте, виникла тимчасова技術чна помилка.</b>\n"
                "Наші інженери вже отримали звіт і виправляють її. Спробуйте, будь ласка, за хвилину!",
                parse_mode="HTML"
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "⚠️ Технічний збій. Інженери вже сповіщені.", show_alert=True
            )
    except Exception as reply_err:
        logging.error(f"Не вдалося відповісти користувачу після помилки: {reply_err}")


async def main():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN не знайдено в змінних оточення!")

    bot = Bot(token=bot_token)
    
    # Зберігаємо екземпляр бота у FastAPI, щоб мати до нього доступ під час надходження грошей
    fastapi_app.state.bot = bot

    # 💥 ЗАПУСКАЄМО ПУЛ POSTGRESQL ТА СТВОРЮЄМО ТАБЛИЦІ
    await init_postgres()

    # НАЛАШТУВАННЯ REDIS ДЛЯ ЗБЕРЕЖЕННЯ СТАНІВ FSM
    redis_host = os.getenv("REDIS_HOST", "redis" if os.path.exists("/.dockerenv") else "localhost")
    redis_client = Redis(host=redis_host, port=6379, decode_responses=True)
    storage = RedisStorage(redis=redis_client)

    dp = Dispatcher(storage=storage)

    # Реєструємо глобальний обробник помилок
    dp.errors.register(global_error_handler)

    # Реєструємо роутери
    dp.include_router(charge_router)
    dp.include_router(user_router)

    logging.basicConfig(level=logging.INFO)
    print("Бот та вебсервер FastAPI успішно ініціалізовані!")

    await bot.delete_webhook(drop_pending_updates=True)
    
    # Конфігурація Uvicorn вебсервера для порту 8000
    uvicorn_config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    uvicorn_server = uvicorn.Server(uvicorn_config)
    
    try:
        # 🔥 ЗАПУСКАЄМО ПАРАЛЕЛЬНО: і Телеграм-поллінг, і Вебсервер Uvicorn
        await asyncio.gather(
            dp.start_polling(bot),
            uvicorn_server.serve()
        )
    finally:
        # 🔒 ЗАКРИВАЄМО ПУЛ ПРИ ЗУПИНЦІ БОТА
        await close_postgres()


if __name__ == "__main__":
    asyncio.run(main())