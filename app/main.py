import asyncio
import logging
from app.core.loader import dp, bot
from app.handlers import user
from app.database.connection import initialize_db 

async def main():
    # 1. Ініціалізуємо базу даних
    await initialize_db()
    
    # 2. Реєструємо роутер
    dp.include_router(user.router)
    
    # 3. Налаштовуємо логування та запуск
    logging.basicConfig(level=logging.INFO)
    print("Бот запущено через нову структуру!")
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())