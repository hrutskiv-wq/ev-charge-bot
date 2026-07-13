import logging
from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from app.database.connection import get_user_data, get_user_transactions

router = Router()

def get_main_menu_keyboard():
    """Генерація головного меню з новою кнопкою Історії"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Зарядка ⚡")],
            [KeyboardButton(text="Як працює? 🤨")],
            [KeyboardButton(text="Ваучер 🎫"), KeyboardButton(text="Історія 📜")],
            [KeyboardButton(text="Online підтримка 📢")]
        ],
        resize_keyboard=True
    )

@router.message(F.text == "/start")
@router.message(F.text == "Головне меню")
async def cmd_start(message: Message):
    user_id = message.from_user.id
    name = message.from_user.first_name or "Водій"
    
    try:
        # Отримуємо актуальний баланс з PostgreSQL
        balance, _ = await get_user_data(user_id)
        
        text = (
            f"👋 <b>Доброго дня, {name}!</b>\n\n"
            f"🔋 Вітаємо в мережі зарядних станцій eVolt UA.\n"
            f"💰 Загальний баланс: <b>{balance:.2f} кВт·год</b>\n\n"
            f"Щоб розпочати сесію, введіть ID станції вручну або скористайтеся меню:"
        )
        await message.answer(text, reply_markup=get_main_menu_keyboard(), parse_mode="HTML")
    except Exception as e:
        logging.error(f"Помилка у старт-хендлері: {e}")
        await message.answer("⚠️ Тимчасова помилка завантаження профілю.")

@router.message(F.text == "Історія 📜")
async def show_history(message: Message):
    user_id = message.from_user.id
    
    # Витягуємо з Ledger-таблиці останні 5 подій
    txs = await get_user_transactions(user_id, limit=5)
    
    if not txs:
        await message.answer("📜 <b>Твоя історія транзакцій поки порожня.</b>\nЗарядись або використай ваучер!", parse_mode="HTML")
        return
        
    history_lines = ["📜 <b>Останні дії по балансу:</b>\n"]
    
    for tx in txs:
        # Форматуємо дату в читаємий європейський вигляд
        date_str = tx['created_at'].strftime("%d.%m %H:%M")
        amount = float(tx['amount'])
        
        # Визначаємо тип операції для емодзі
        if tx['type'] == 'deposit':
            sign = f"🟢 +{amount:.2f} кВт·год"
        else:
            sign = f"🔴 -{amount:.2f} кВт·год"
            
        desc = tx['description'] or "Сесія зарядки"
        
        history_lines.append(f"⏱ <i>{date_str}</i>\n└ {sign} ({desc})\n")
        
    final_text = "\n".join(history_lines)
    await message.answer(final_text, parse_mode="HTML")
