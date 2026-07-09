import logging
import asyncio
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import CallbackData, Command
from aiogram.fsm.context import FSMContext

# Імпортуємо кастомні стани
from app.states.charge_states import ChargingStates

charge_router = Router()

class ConnectorCallback(CallbackData, prefix="station_connector"):
    station_id: str
    id_connector: str
    connector_type: str


# ⌨️ СТВОРЮЄМО НИЖНЄ МЕНЮ НА ЧАС ЗАРЯДКИ
charging_reply_menu = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📊 Статус"),
            KeyboardButton(text="🛑 Зупинити зарядку")
        ]
    ],
    resize_keyboard=True,  # Робить кнопки акуратними, а не на весь екран
    one_time_keyboard=False
)


# ХЕНДЛЕР 1: Обробка введення ID станції
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
            display_text = f"🔌 CCS (Type 2) [Порт {ccs_counter}] ({power} кВт)"
        else:
            display_text = f"🔌 {text_type} ({power} кВт)"
            
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
            f"Оберіть кабель, який підключено до авто:"
        ),
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# ФОНОВА ТАСКА: Автоматична зупинка через 15 секунд
async def simulate_station_auto_stop(chat_id: int, message_bot, target_state: FSMContext, conn_id: str):
    await asyncio.sleep(15)
    
    current_state = await target_state.get_state()
    if current_state == ChargingStates.charging_active:
        await target_state.clear()  # Очищуємо FSM
        
        try:
            # ReplyKeyboardRemove() ховає великі кнопки і повертає стандартну клавіатуру
            await message_bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔔 <b>Сповіщення від eVolt UA</b>\n\n"
                    f"🔋 <b>Ваш електромобіль повністю зарядився до 100%!</b>\n"
                    f"Сесію успішно завершено автоматично.\n\n"
                    f"🏁 Порт: <code>{conn_id}</code>"
                ),
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e:
            logging.error(f"Не вдалося надіслати автозупинку: {e}")


# ХЕНДЛЕР 2: Клік на кнопку роз'єму (Вмикаємо зарядку та підкидаємо НИЖНЄ МЕНЮ)
@charge_router.callback_query(ConnectorCallback.filter())
async def handle_connector_selection(call: CallbackQuery, callback_data: ConnectorCallback, state: FSMContext):
    id_connector = callback_data.id_connector
    
    await state.set_state(ChargingStates.charging_active)
    await state.update_data(active_connector_id=id_connector)
    await call.answer("Автентифікація сесії...")
    
    # Редагуємо інлайн-повідомлення
    await call.message.edit_text(
        f"🔋 <b>Зарядка успішно активована!</b>\n\n"
        f"🔌 Порт ID: {id_connector}\n"
        f"Статус: Заряджання автомобіля...\n\n"
        f"<i>Для керування використовуйте великі кнопки внизу екрана.</i>",
        parse_mode="HTML"
    )
    
    # 💥 НАДСИЛАЄМО НОВЕ ПОВІДОМЛЕННЯ, ЯКЕ АКТИВУЄ НИЖНЄ МЕНЮ КНОПОК
    await call.message.answer(
        text="🎛️ <b>Пульт керування зарядкою активовано:</b>",
        reply_markup=charging_reply_menu,
        parse_mode="HTML"
    )
    
    asyncio.create_task(
        simulate_station_auto_stop(
            chat_id=call.from_user.id,
            message_bot=call.bot,
            target_state=state,
            conn_id=id_connector
        )
    )


# ХЕНДЛЕР 3: Ручна зупинка сесії (через інлайн-кнопку, команду /stop або кнопку меню "🛑 Зупинити зарядку")
@charge_router.callback_query(ChargingStates.charging_active, F.data == "stop_charging")
@charge_router.message(ChargingStates.charging_active, Command("stop"))
@charge_router.message(ChargingStates.charging_active, F.text == "🛑 Зупинити зарядку")
async def handle_stop_charging(event: CallbackQuery | Message, state: FSMContext):
    user_data = await state.get_data()
    connector_id = user_data.get("active_connector_id", "Невідомий")
    
    await state.clear()  # Скидаємо стан FSM
    
    text_response = (
        f"🛑 <b>Зарядну сесію порту [{connector_id}] успішно завершено!</b>\n\n"
        f"Тепер ви можете знову ввести новий ID станції."
    )
    
    # Перевіряємо, звідки прийшов запит (з кліку на кнопку чи з тексту)
    if isinstance(event, CallbackQuery):
        await event.answer("Зупиняємо...")
        await event.message.answer(text_response, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    else:
        await event.answer(text_response, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())


# ХЕНДЛЕР 4: Перевірка статусу (через команду /status або кнопку меню "📊 Статус")
@charge_router.message(ChargingStates.charging_active, Command("status"))
@charge_router.message(ChargingStates.charging_active, F.text == "📊 Статус")
async def command_status_charging(message: Message, state: FSMContext):
    user_data = await state.get_data()
    connector_id = user_data.get("active_connector_id", "Невідомий")
    
    await message.answer(
        f"⏳ <b>Поточний статус сесії:</b>\n\n"
        f"🔌 Активний порт: <code>{connector_id}</code>\n"
        f"⚡ Процес: Автомобіль заряджається.",
        parse_mode="HTML"
    )


# ХЕНДЛЕР 5: Перехоплювач будь-якого іншого тексту під час зарядки
@charge_router.message(ChargingStates.charging_active)
async def process_text_during_charge(message: Message):
    await message.answer(
        "🚨 <b>У вас є активна зарядна сесія!</b>\n\n"
        "Будь ласка, використовуйте великі кнопки <b>📊 Статус</b> або <b>🛑 Зупинити зарядку</b> внизу екрана.",
        parse_mode="HTML"
    )