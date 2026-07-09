import logging
import asyncio
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext

# 💥 Імпортуємо чисті функції роботи з PostgreSQL (без грн та тарифів)
from app.database.connection import (
    get_user_data, update_user_balance, get_station_by_id
)
from app.states.charge_states import ChargingStates

charge_router = Router()

class ConnectorCallback(CallbackData, prefix="station_connector"):
    station_id: str
    id_connector: str
    connector_type: str

charging_reply_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📊 Статус"), KeyboardButton(text="🛑 Зупинити зарядку")]],
    resize_keyboard=True
)


# ХЕНДЛЕР 1: Обробка введення ID станції (Динамічні роз'єми з БД)
@charge_router.message(F.text.regexp(r"OCM-\d+"))
async def handle_station_id(message: Message):
    station_id = message.text.strip()
    
    # Перевіряємо залишок пакета кВт·год перед тим, як дозволити запуск
    balance, discount = await get_user_data(message.from_user.id)
    if balance <= 0:
        await message.answer(
            f"❌ <b>Запуск заблоковано!</b>\n\n"
            f"🔋 Ваш баланс: <code>{balance} кВт·год</code>.\n"
            f"Для активації станції, будь ласка, придбайте тарифний пакет у меню ваучерів.",
            parse_mode="HTML"
        )
        return

    # Отримуємо реальні дані станції з бази даних PostgreSQL
    station_data = await get_station_by_id(station_id)
    buttons = []
    
    if station_data and station_data[2]:  # station_data[2] — це рядок із роз'ємами
        raw_connectors = [c.strip() for c in station_data[2].split(",") if c.strip()]
        for i, conn_name in enumerate(raw_connectors):
            short_type = conn_name.split("(")[0].strip()[:10]
            conn_id = f"P{i+1}"  # Унікальний ID порту (P1, P2...)
            display_text = f"🔌 Увімкнути {conn_name}"
            buttons.append([
                InlineKeyboardButton(
                    text=display_text,
                    callback_data=ConnectorCallback(
                        station_id=station_id, 
                        id_connector=conn_id, 
                        connector_type=short_type
                    ).pack()
                )
            ])
    else:
        # Резервний дефолтний варіант, якщо станції тимчасово немає в локальній БД
        mock_api_response = [
            {"id": "4501", "type": "CCS (Type 2)", "power": "120"},
            {"id": "4502", "type": "CHAdeMO", "power": "50"}
        ]
        for conn in mock_api_response:
            buttons.append([
                InlineKeyboardButton(
                    text=f"🔌 Увімкнути {conn['type']} ({conn['power']} кВт)",
                    callback_data=ConnectorCallback(station_id=station_id, id_connector=conn['id'], connector_type=conn['type']).pack()
                )
            ])
        
    await message.answer(
        text=f"⚡ <b>Станція ID: {station_id}</b>\n\nОберіть кабель для початку зарядки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


# ФОНОВА ТАСКА: Автоматична зупинка (Списання чистих кВт·год)
async def simulate_station_auto_stop(chat_id: int, message_bot, target_state: FSMContext, conn_id: str):
    await asyncio.sleep(15)  # Симулюємо сесію заправки
    
    current_state = await target_state.get_state()
    if current_state == ChargingStates.charging_active:
        await target_state.clear()
        
        # Симулюємо споживання в кВт·год
        consumed_kwh = 8.5
        
        # 💥 Списуємо чисті кВт·год з бази даних (передаємо мінусове значення)
        await update_user_balance(chat_id, -consumed_kwh, t_type="charge_session")
        
        # Беремо свіжий пакетний баланс водія після списання
        new_balance, _ = await get_user_data(chat_id)
        
        try:
            await message_bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔔 <b>Сесію завершено автоматично з боку станції!</b>\n\n"
                    f"🏁 Порт: <code>{conn_id}</code>\n"
                    f"🔋 Спожито за сесію: <b>{consumed_kwh} кВт·год</b>\n"
                    f"📉 Ваш залишок пакета: <b>{new_balance} кВт·год</b>\n\n"
                    f"Дякуємо, що обираєте мережу eVolt UA!"
                ),
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e:
            logging.error(f"Помилка надсилання сповіщення: {e}")


# ХЕНДЛЕР 2: Клік на роз'єм
@charge_router.callback_query(ConnectorCallback.filter())
async def handle_connector_selection(call: CallbackQuery, callback_data: ConnectorCallback, state:FSMContext):
    id_connector = callback_data.id_connector
    await state.set_state(ChargingStates.charging_active)
    await state.update_data(active_connector_id=id_connector)
    await call.answer("Автентифікація сесії...")
    
    await call.message.edit_text(
        f"🔋 <b>Зарядка успішно активована!</b>\n🔌 Порт ID: {id_connector}\n\n<i>Кіловат-години будуть списані автоматично в кінці сесії.</i>",
        parse_mode="HTML"
    )
    await call.message.answer(text="🎛️ Пульт керування:", reply_markup=charging_reply_menu)
    
    asyncio.create_task(simulate_station_auto_stop(chat_id=call.from_user.id, message_bot=call.bot, target_state=state, conn_id=id_connector))


# ХЕНДЛЕР 3: Ручна зупинка водієм (Списання чистих кВт·год)
@charge_router.message(ChargingStates.charging_active, F.text == "🛑 Зупинити зарядку")
@charge_router.message(ChargingStates.charging_active, Command("stop"))
async def handle_stop_charging(message: Message, state: FSMContext):
    user_data = await state.get_data()
    connector_id = user_data.get("active_connector_id", "Невідомий")
    await state.clear()
    
    # При ручній зупинці списуємо менше, наприклад 2.0 кВт·год
    consumed_kwh = 2.0
    
    # 💥 Списуємо чисті кВт·год з бази danych
    await update_user_balance(message.from_user.id, -consumed_kwh, t_type="charge_manual_stop")
    new_balance, _ = await get_user_data(message.from_user.id)
    
    await message.answer(
        f"🛑 <b>Зарядку зупинено водієм!</b>\n\n"
        f"🏁 Порт: <code>{connector_id}</code>\n"
        f"🔋 Спожито за сесію: <b>{consumed_kwh} кВт·год</b>\n"
        f"📉 Ваш залишок пакета: <b>{new_balance} кВт·год</b>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


# ХЕНДЛЕР 4: Статус
@charge_router.message(ChargingStates.charging_active, F.text == "📊 Статус")
@charge_router.message(ChargingStates.charging_active, Command("status"))
async def command_status_charging(message: Message, state: FSMContext):
    user_data = await state.get_data()
    await message.answer(f"⏳ <b>Автомобіль заряджається!</b>\n🔌 Порт: <code>{user_data.get('active_connector_id')}</code>", parse_mode="HTML")


# ХЕНДЛЕР 5: Заглушка текстів під час зарядки
@charge_router.message(ChargingStates.charging_active)
async def process_text_during_charge(message: Message):
    await message.answer("🚨 <b>Йде зарядка!</b> Використовуйте нижні кнопки керування.", parse_mode="HTML")