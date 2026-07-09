import logging
import asyncio
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CallbackData, Command  # Додали Command фільтр
from aiogram.fsm.context import FSMContext

# Імпортуємо кастомні стани
from app.states.charge_states import ChargingStates

charge_router = Router()

class ConnectorCallback(CallbackData, prefix="station_connector"):
    station_id: str
    id_connector: str
    connector_type: str


# ХЕНДЛЕР 1: Обробка введення ID станції (OCM-307584)
@charge_router.message(F.text.regexp(r"OCM-\d+"))
async def handle_station_id(message: Message):
    station_id = message.text.strip()
    logging.info(f"Запит роз'ємів для станції: {station_id}")
    
    mock_api_response = [
        {"id": "4501", "type": "CCS (Type 2)", "power": "240"},
        {"id": "4502", "type": "CCS (Type 2)", "power": "240"},
        {"id": "4503", "type": "GB-T DC", "power": "160"},
        {"id": "4504", "type": "Type 2", "power": "22"}
    ]
    
    buttons = []
    ccs_counter = 0
    
    for conn in mock_api_response:
        text_type = conn['type']
        power = conn['power']
        
        if text_type == "CCS (Type 2)":
            ccs_counter += 1
            display_text = f"🔌 Увімкнути CCS (Type 2) [Порт {ccs_counter}] ({power} кВт)"
        else:
            display_text = f"🔌 Увімкнути {text_type} ({power} кВт)"
            
        buttons.append([
            InlineKeyboardButton(
                text=display_text,
                callback_data=ConnectorCallback(
                    station_id=station_id,
                    id_connector=str(conn['id']),
                    connector_type=text_type
                ).pack()
            )
        ])
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.answer(
        text=(
            f"⚡ <b>Комплекс: Зубра HyperCharger</b>\n"
            f"🚉 Станція ID: <code>{station_id}</code>\n\n"
            f"Будь ласка, оберіть роз'єм (кабель), який ви підключили до свого електромобіля:"
        ),
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# ФОНОВА ТАСКА: Симулює автоматичну зупинку від залізяки через 15 секунд
async def simulate_station_auto_stop(chat_id: int, message_bot, target_state: FSMContext, conn_id: str):
    await asyncio.sleep(15)  # Імітуємо 15 секунд активної зарядки
    
    current_state = await target_state.get_state()
    if current_state == ChargingStates.charging_active:
        await target_state.clear()  # Скидаємо стан FSM "ззовні"
        
        try:
            await message_bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔔 <b>Сповіщення від eVolt UA</b>\n\n"
                    f"🔋 <b>Ваш електромобіль повністю зарядився до 100%!</b>\n"
                    f"Сесію успешно завершено автоматично з боку станції.\n\n"
                    f"🏁 Порт: <code>{conn_id}</code>\n"
                    f"Тепер ви можете знову ввести новий ID станції."
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не вдалося надіслати сповіщення про автозупинку: {e}")


# ХЕНДЛЕР 2: Обробка кліку на кнопку роз'єму (Вмикаємо зарядку та стейт)
@charge_router.callback_query(ConnectorCallback.filter())
async def handle_connector_selection(call: CallbackQuery, callback_data: ConnectorCallback, state: FSMContext):
    station_id = callback_data.station_id
    id_connector = callback_data.id_connector
    
    logging.info(f"Запуск порту ID {id_connector} на станції {station_id}")
    
    # Активуємо стан зарядки в Redis
    await state.set_state(ChargingStates.charging_active)
    await state.update_data(active_connector_id=id_connector)
    
    await call.answer("Автентифікація сесії...")
    
    stop_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Зупинити зарядку", callback_data="stop_charging")]
    ])
    
    await call.message.edit_text(
        f"🔋 <b>Зарядка успішно активована!</b>\n\n"
        f"⚡ Комплекс: Зубра HyperCharger\n"
        f"🔌 Порт ID: {id_connector}\n"
        f"Статус: Заряджання автомобіля...\n\n"
        f"<i>Всі інші функції бота заблоковано. Ви можете використовувати команди /status або /stop.</i>",
        parse_mode="HTML",
        reply_markup=stop_keyboard
    )
    
    # Запускаємо асинхронну фонову таску автозупинки станції
    asyncio.create_task(
        simulate_station_auto_stop(
            chat_id=call.from_user.id,
            message_bot=call.bot,
            target_state=state,
            conn_id=id_connector
        )
    )


# ХЕНДЛЕР 3: Ручна зупинка сесії водієм через кнопку
@charge_router.callback_query(ChargingStates.charging_active, F.data == "stop_charging")
async def handle_stop_charging(call: CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    connector_id = user_data.get("active_connector_id", "Невідомий")
    
    await call.answer("Зупиняємо сесію...")
    await state.clear()  # Скидаємо стан
    
    await call.message.edit_text(
        f"🛑 <b>Зарядну сесію порту [{connector_id}] успішно завершено!</b>\n\n"
        f"Дякуємо, що скористалися eVolt UA. Тепер ви можете знову ввести новий ID станції.",
        parse_mode="HTML"
    )


# ХЕНДЛЕР 4: Примусова текстова зупинка через команду /stop під час зарядки
@charge_router.message(ChargingStates.charging_active, Command("stop"))
async def command_stop_charging(message: Message, state: FSMContext):
    user_data = await state.get_data()
    connector_id = user_data.get("active_connector_id", "Невідомий")
    
    await state.clear()  # Очищуємо стан FSM в Redis
    await message.answer(
        f"🛑 <b>Зарядну сесію порту [{connector_id}] примусово зупинено через команду /stop!</b>\n\n"
        f"Станцію звільнено. Тепер ви можете ввести новий ID станції.",
        parse_mode="HTML"
    )


# ХЕНДЛЕР 5: Перевірка поточного статусу зарядки через команду /status
@charge_router.message(ChargingStates.charging_active, Command("status"))
async def command_status_charging(message: Message, state: FSMContext):
    user_data = await state.get_data()
    connector_id = user_data.get("active_connector_id", "Невідомий")
    
    await message.answer(
        f"⏳ <b>Поточний статус сесії:</b>\n\n"
        f"🔌 Активний порт: <code>{connector_id}</code>\n"
        f"⚡ Процес: Автомобіль зараз отримує енергію.\n\n"
        f"<i>Якщо ви втратили кнопку зупинки, надішліть команду /stop для переривання процесу.</i>",
        parse_mode="HTML"
    )


# ХЕНДЛЕР 6: Заглушка-перехоплювач для будь-якого іншого тексту під час зарядки
@charge_router.message(ChargingStates.charging_active)
async def process_text_during_charge(message: Message):
    await message.answer(
        "🚨 <b>У вас є активна зарядна сесія!</b>\n\n"
        "Введення нових ID станцій або тексту заблоковано до завершення процесу.\n\n"
        "ℹ️ <i>Щоб перевірити показники, введіть /status\n"
        "ℹ️ Щоб примусово вимкнути кабель, введіть /stop</i>",
        parse_mode="HTML"
    )