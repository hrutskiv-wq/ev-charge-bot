import logging
import asyncio
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters.callback_data import CallbackData

charge_router = Router()

# 1. Оновлюємо фабрику: тепер вона вміє зберігати точний ID конкретного порту
class ConnectorCallback(CallbackData, prefix="station_connector"):
    station_id: str
    id_connector: str  # Додали унікальний ID порту (наприклад, "4501")
    connector_type: str


# 2. ХЕНДЛЕР 1: Обробка введення ID станції (наприклад, OCM-307584)
@charge_router.message(F.text.regexp(r"OCM-\d+"))
async def handle_station_id(message: Message):
    station_id = message.text.strip()
    logging.info(f"Запит роз'ємів для станції: {station_id}")
    
    # СИМУЛЯЦІЯ ВІДПОВІДІ API (як для станції в Зубрі):
    # У реальному житті цей список прилетить із сервера еVolt/Toka
    mock_api_response = [
        {"id": "4501", "type": "CCS (Type 2)", "power": 240},
        {"id": "4502", "type": "CCS (Type 2)", "power": 240},  # Другий такий самий кабель
        {"id": "4503", "type": "GB-T DC", "power": 240},
        {"id": "4504", "type": "Type 2", "power": 22}
    ]
    
    buttons = []
    ccs_counter = 0  # Лічильник суто для гарного маркування портів CCS
    
    for conn in mock_api_response:
        text_type = conn['type']
        power = conn['power']
        
        # Якщо це CCS і це швидка зарядка, нумеруємо порти, щоб водій не плутався
        if text_type == "CCS (Type 2)" and power > 50:
            ccs_counter += 1
            display_text = f"🔌 Увімкнути CCS (Type 2) [Порт {ccs_counter}] ({power} кВт)"
        else:
            display_text = f"🔌 Увімкнути {text_type} ({power} кВт)"
            
        buttons.append([
            InlineKeyboardButton(
                text=display_text,
                # Зашиваємо точні дані в кнопку
                callback_data=ConnectorCallback(
                    station_id=station_id,
                    id_connector=str(conn['id']),  # Тепер передається точний ID порту!
                    connector_type=text_type
                ).pack()
            )
        ])
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.answer(
        text=(
            f"⚡️ <b>Комплекс: Зубра HyperCharger</b>\n"
            f"Станція ID: <code>{station_id}</code>\n\n"
            f"Будь ласка, оберіть роз'єм (кабель), який ви підключили до свого електромобіля:"
        ),
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# 3. ХЕНДЛЕР 2: Обробка кліку на кнопку роз'єму
@charge_router.callback_query(ConnectorCallback.filter())
async def handle_connector_selection(call: CallbackQuery, callback_data: ConnectorCallback):
    station_id = callback_data.station_id
    id_connector = callback_data.id_connector  # Отримуємо точний ID кабелю
    connector_type = callback_data.connector_type 
    # Виводимо в логи сервера точну інформацію, який кабель запускається
    logging.info(f"Запуск порту ID {id_connector} ({connector_type}) на станції {station_id}")
    
    await call.answer("Автентифікація сесії...")
    
    await call.message.edit_text(
        text=(
            f"⏳ <b>Авторизація сесії...</b>\n"
            f"Підключення до кабелю [ID: {id_connector}] на станції {station_id}...\n\n"
            f"Будь ласка, зачекайте."
        ),
        parse_mode="HTML"
    )
    
    # Тут у майбутньому буде реальний запит на запуск:
    # await api.start_charging(station_id, id_connector)
    
    await asyncio.sleep(1.5)
    
    await call.message.edit_text(
        text=(
            f"✅ <b>Зарядку успішно активовано!</b>\n\n"
            f"• Станція: {station_id}\n"
            f"• Порт у системі: ID {id_connector} ({connector_type})\n"
            f"• Списано (резерв): 5.00 кВт·год\n"
            f"💰 Залишок на балансі: 163.00 кВт·год"
        ),
        parse_mode="HTML"
    )