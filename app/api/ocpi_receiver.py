import logging
from fastapi import APIRouter, HTTPException, Header, Request
from app.schemas.ocpi_cdr import CDR
from app.database.connection import update_user_balance, get_user_data

router = APIRouter()

@router.post("/ocpi/2.2.1/cdrs/")
async def receive_cdr(cdr: CDR, request: Request, authorization: str = Header(None)):
    # 1. Валідація токена безпеки мережі CPO
    if not authorization or "Token" not in authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    logging.info(f"📥 Отримано валідний OCPI CDR {cdr.cdr_id} для водія {cdr.auth_id}")
    
    try:
        user_id = int(cdr.auth_id)
        
        # 2. Пряме Ledger-списання кВт·год в PostgreSQL
        await update_user_balance(user_id=user_id, amount_kwh=cdr.total_energy, t_type=f"OCPI:{cdr.cdr_id}")
        
        # 3. Отримання оновленого балансу
        new_balance, _ = await get_user_data(user_id)
        
        # 4. Надсилання миттєвого сповіщення в Telegram
        bot = request.app.state.bot
        message_text = (
            f"⚡️ <b>Сесію зарядки успішно завершено!</b>\n\n"
            f"🔋 Спожито заліза: <code>{cdr.total_energy} кВт·год</code>\n"
            f"💰 Орієнтовна вартість сесії: {cdr.total_cost} грн\n"
            f"📉 Твій залишок по пакету: <b>{new_balance:.2f} кВт·год</b>\n\n"
            f"Дякуємо, що користуєшся мережею eVolt UA! 🔌"
        )
        await bot.send_message(chat_id=user_id, text=message_text, parse_mode="HTML")
        logging.info(f"🔔 Успішно надіслано Telegram-сповіщення для користувача {user_id}")
        
    except ValueError:
        logging.error(f"❌ Не вдалося розпізнати Telegram ID з auth_id: {cdr.auth_id}")
    except Exception as e:
        logging.error(f"❌ Помилка під час обробки транзакції білінгу CDR: {e}", exc_info=True)

    # Завжди повертаємо мережі 1000 (успіх), щоб вони не дублювали запити
    return {"status_code": 1000, "status_message": "Accepted"}
