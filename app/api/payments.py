import logging
import json
from fastapi import APIRouter, Request, Response, status

# Імпортуємо централізоване підключення та тариф
from app.database import connection
from app.database.connection import PRICE_PER_KWH

logger = logging.getLogger(__name__)

# Створюємо роутер для FastAPI
payments_router = APIRouter()

# Підтримуємо обидва варіанти виклику ендпоінту
@payments_router.post("/webhook/monobank")
@payments_router.post("/webhook/mono")
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
        
        # 1. Отримуємо унікальний ID транзакції від Монобанку для захисту від дублікатів
        transaction_id = item.get("id")
        if not transaction_id:
            logger.error("Monobank Webhook: у виписці відсутній унікальний id транзакції")
            return Response(status_code=status.HTTP_200_OK)

        # Монобанк присилає суму в копійках. Переводимо в чисті гривні.
        raw_amount = item.get("amount", 0)
        amount_uah = raw_amount / 100
        
        # Якщо сума мінусова або нуль — це списання (ігноруємо)
        if amount_uah <= 0:
            return Response(status_code=status.HTTP_200_OK)
            
        # Дістаємо коментар до платежу, куди мы зашили Telegram ID водія
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
            
            # --- АТОМАРНИЙ ЗАПИС У ПОСТГРЕС (НОВА СХЕМА) ---
            try:
                async with connection.db_pool.acquire() as conn:
                    async with conn.transaction():
                        
                        # Перевіряємо, чи цей платіж вже оброблявся раніше
                        existing_payment = await conn.fetchrow(
                            "SELECT id FROM payments WHERE invoice_id = $1", 
                            transaction_id
                        )
                        
                        if existing_payment:
                            logger.info(f"Транзакція Monobank {transaction_id} вже була успішно оброблена раніше. Пропускаємо.")
                            return Response(status_code=status.HTTP_200_OK)
                        
                        # Оскільки це Моно-Банка, платіж фіксується одразу як успішний ('success')
                        payment_id = await conn.fetchval(
                            """
                            INSERT INTO payments (user_id, invoice_id, amount, provider, status, payload, created_at, updated_at)
                            VALUES ($1, $2, $3, 'monobank', 'success', $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            RETURNING id
                            """,
                            user_id, transaction_id, amount_uah, json.dumps(payload)
                        )
                        
                        # Створюємо фінансовий лог нарахування внутрішнього балансу
                        description = f"Успішна оплата пакету: {kwh_to_add} кВт·год через Банку Monobank"
                        await conn.execute(
                            """
                            INSERT INTO kw_transactions (user_id, type, amount, payment_id, description, created_at)
                            VALUES ($1, 'deposit', $2, $3, $4, CURRENT_TIMESTAMP)
                            """,
                            user_id, kwh_to_add, payment_id, description
                        )
                        
                logger.info(f"Успішно фінансово зараховано {kwh_to_add} кВт·год для користувача {user_id} через Банку Моно")
                
            except Exception as db_err:
                logger.error(f"Критична помилка транзакції бази даних для платежу {transaction_id}: {db_err}")
                return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            # --- ВІДПРАВКА МИТТЄВОГО СПОВІЩЕННЯ В ТЕЛЕГРАМ ---
            try:
                bot = request.app.state.bot
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🎉 <b>Тарифний пакет активовано!</b>\n\n"
                        f"💳 Отримано платіж через Monobank: <b>{amount_uah:.2f} грн</b>\n"
                        f"🔋 На Ваш рахунок успешно зараховано: <b>{kwh_to_add} кВт·год</b>\n\n"
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
            
    return Response(status_code=status.HTTP_200_OK)
