import logging
from fastapi import APIRouter, Request, Response, status

# Імпортуємо функції роботи з базою даних та тариф
from app.database.connection import update_user_balance, PRICE_PER_KWH

logger = logging.getLogger(__name__)

# Створюємо роутер для FastAPI
payments_router = APIRouter()

@payments_router.post("/webhook/monobank")
async def monobank_webhook(request: Request):
    """
    Ендпоінт (Webhook), куди сервери Monobank будуть миттєво 
    надсилати інформацію про кожну гривню, що влетіла в Банку.
    """
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Помилка парсингу JSON від Monobank: {e}")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    
    # Перевіряємо, чи це тип події - новий запис у виписці (StatementItem)
    if payload.get("type") == "StatementItem":
        data = payload.get("data", {})
        item = data.get("statementItem", {})
        
        # Монобанк присилає суму в копійках. Переводимо в чисті гривні.
        # (Надходження йдуть зі знаком плюс, витрати зі знаком мінус)
        raw_amount = item.get("amount", 0)
        amount_uah = raw_amount / 100
        
        # Якщо сума мінусова або нуль — це списання (ігноруємо)
        if amount_uah <= 0:
            return Response(status_code=status.HTTP_200_OK)
            
        # Дістаємо коментар до платежу, куди ми зашили Telegram ID водія
        comment_raw = item.get("comment", "").strip()
        
        # Перевіряємо, чи в коментарі дійсно лежить числовий Telegram ID
        if comment_raw.isdigit():
            user_id = int(comment_raw)
            
            # Нараховуємо пакетні кВт·год відповідно до сплаченої суми
            if amount_uah == 750:
                kwh_to_add = 50.0
            elif amount_uah == 1350:
                kwh_to_add = 100.0
            else:
                # На випадок, якщо водій скинув іншу суму вручну (пропорційно тарифу)
                kwh_to_add = round(amount_uah / PRICE_PER_KWH, 2)
            
            # Конвертуємо кіловати у внутрішні одиниці бази даних (множимо на 15)
            # 750 грн = 750 одиниць бази, що дасть водієві рівно 50 кВт·год
            db_units = kwh_to_add * PRICE_PER_KWH
            
            # 💥 Зараховуємо кошти в PostgreSQL
            await update_user_balance(user_id, db_units, t_type="monobank_jar")
            logger.info(f"Успішно зараховано {kwh_to_add} кВт·год для користувача {user_id} через Банку Моно")
            
            # 🔔 НАДВАЖЛИВО: Локальний імпорт бота всередині функції.
            # Це рятує додаток від Circular Import при старті сервера!
            try:
                from server import bot
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🎉 <b>Тарифний пакет активовано!</b>\n\n"
                        f"💳 Отримано платіж через Monobank: <b>{amount_uah:.2f} грн</b>\n"
                        f"🔋 На Ваш рахунок успішно зараховано: <b>{kwh_to_add} кВт·год</b>\n\n"
                        f"⚡ Можете надсилати ID станції. Приємної зарядки з eVolt UA!"
                    ),
                    parse_mode="HTML"
                )
            except Exception as tg_err:
                logger.error(f"Не вдалося надіслати сповіщення водію {user_id} в Telegram: {tg_err}")
        else:
            logger.warning(
                f"Отримано платіж на {amount_uah} грн, але коментар не є Telegram ID: '{comment_raw}'"
            )
            
    # Monobank вимагає, щоб ми завжди повертали статус 200 OK у відповідь на його вебхук,
    # інакше він подумає, що наш сервер впав, і почне спамити повторними запитами.
    return Response(status_code=status.HTTP_200_OK)