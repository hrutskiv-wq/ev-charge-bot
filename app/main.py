import os
import logging
import asyncio
import traceback
import html  # Додано для безпечного екранування HTML-символів
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

# Імпортуємо роутери
from app.handlers.user import router as user_router
from app.handlers.charge import charge_router

    # ГЛОБАЛЬНИЙ ОБРОБНИК ПОМИЛОК
async def global_error_handler(event: ErrorEvent, bot: Bot):
    exception = event.exception
    update = event.update
    
    # --- ДОДАЙ ЦЕЙ БЛОК СЮДИ ---
    # Ігноруємо дублюючі кліки, щоб не спамити в чат логів
    if "message is not modified" in str(exception):
        try:
            if update.callback_query:
                await update.callback_query.answer() # Просто знімаємо "годинничок" завантаження з кнопки
        except Exception:
            pass
        return # Виходимо з функції, звіт в чат не надсилаємо
    # ----------------------------
    
    # 1. Логуємо помилку в консоль сервера
    logging.error(f"💥 Критична помилка: {exception}", exc_info=True)
    ...
    # 1. Логуємо помилку в консоль сервера
    logging.error(f"💥 Критична помилка: {exception}", exc_info=True)
    
    # 2. Формуємо Traceback для відправки в адмін-чат
    tb_lines = traceback.format_exception(type(exception), exception, exception.__traceback__)
    tb_text = "".join(tb_lines)
    
    # Обрізаємо текст, якщо він довший за ліміт Telegram (4096 символів)
    if len(tb_text) > 3500:
        tb_text = tb_text[-3500:]
        
    logs_chat_id = os.getenv("LOGS_CHAT_ID")
    
    if logs_chat_id:
        # БЕЗПЕКА: Екрануємо символи <, >, &, щоб Telegram не сварився на "Unsupported start tag"
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

    # 3. Ввічливо відповідаємо водію в Telegram, щоб інтерфейс не зависав
    try:
        if update.message:
            await update.message.answer(
                "⚠️ <b>Вибачте, виникла тимчасова технічна помилка.</b>\n"
                "Наші інженери вже отримали звіт і виправляють її. Спробуйте, будь ласка, за хвилину!"
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
    dp = Dispatcher()

    # 1. Реєструємо глобальний обробник помилок
    dp.errors.register(global_error_handler)

    # 2. Реєструємо роутери (charge_router має вищий пріоритет)
    dp.include_router(charge_router)
    dp.include_router(user_router)

    # 3. Налаштовуємо логування
    logging.basicConfig(level=logging.INFO)
    print("Бот запущено через нову структуру!")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())