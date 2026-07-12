import os
import asyncio
import logging
import time
import re  # Імпортуємо для пошуку множників типу x2
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext

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
    keyboard=[[KeyboardButton(text="📊 Status"), KeyboardButton(text="🛑 Stop Charging")]],
    resize_keyboard=True
)

main_reply_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Зарядка ⚡")],
        [KeyboardButton(text="Як працює? 🤨")],
        [KeyboardButton(text="Ваучер 🎫"), KeyboardButton(text="Online підтримка 📢")]
    ],
    resize_keyboard=True
)

# --- ХЕНДЛЕР 1: ID станції ---
@charge_router.message(F.text.regexp(r"OCM-\d+"))
async def handle_station_id(message: Message):
    station_id = message.text.strip()
    
    balance_kwh, discount = await get_user_data(message.from_user.id)
    
    if balance_kwh <= 0:
        await message.answer(
            f"❌ <b>Запуск заблоковано!</b>\n\n"
            f"🔋 Ваш загальний баланс: <code>{balance_kwh:.2f} кВт·год</code>.\n"
            f"Будь ласка, придбайте тарифний пакет у меню ваучерів 🎫.",
            parse_mode="HTML"
        )
        return

    station_data = await get_station_by_id(station_id)
    buttons = []
    
    if station_data and station_data[2]:
        # 1. Нормалізуємо розділювачі
        normalized_connectors = station_data[2].replace(";", ",")
        raw_connectors = [c.strip() for c in normalized_connectors.split(",") if c.strip()]
        
        # 2. Розмножуємо порти, якщо є приписка x2, x3 тощо
        expanded_connectors = []
        for conn in raw_connectors:
            match = re.search(r'\s*x\s*(\d+)\s*$', conn)
            if match:
                count = int(match.group(1))
                clean_name = conn[:match.start()].strip()
                for idx in range(count):
                    expanded_connectors.append(f"{clean_name} #{idx+1}")
            else:
                expanded_connectors.append(conn)
        
        # 3. Генеруємо кнопки для кожного фізичного порту
        for i, conn_name in enumerate(expanded_connectors):
            short_type = conn_name.split("(")[0].strip()[:10]
            conn_id = f"P{i+1}"
            buttons.append([
                InlineKeyboardButton(
                    text=f"🔌 Увімкнути {conn_name}", 
                    callback_data=ConnectorCallback(station_id=station_id, id_connector=conn_id, connector_type=short_type).pack()
                )
            ])
    else:
        # Резервна заглушка, якщо станції взагалі немає в базі
        mock_api_response = ["CCS (Type 2) (120 кВт)", "Type 2 (22 кВт)"]
        for i, conn_name in enumerate(mock_api_response):
            conn_id = f"P{i+1}"
            buttons.append([
                InlineKeyboardButton(
                    text=f"🔌 Увімкнути {conn_name}", 
                    callback_data=ConnectorCallback(station_id=station_id, id_connector=conn_id, connector_type="Backup").pack()
                )
            ])
        
    await message.answer(
        text=f"⚡ <b>Станція ID: {station_id}</b>\n\nОберіть кабель:", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), 
        parse_mode="HTML"
    )

# --- ФОНОВА ТАСКА: Автоматична зупинка ---
async def simulate_station_auto_stop(chat_id: int, message_bot, target_state: FSMContext, conn_id: str):
    await asyncio.sleep(15)
    current_state = await target_state.get_state()
    
    if current_state == ChargingStates.charging_active:
        user_data = await target_state.get_data()
        started_at = user_data.get("started_at")
        
        await target_state.clear()
        
        consumed_kwh = 8.5
        await update_user_balance(chat_id, consumed_kwh, t_type="charge_session")
        balance_kwh, _ = await get_user_data(chat_id)
        
        if started_at:
            duration_seconds = int(time.time() - started_at)
            minutes = duration_seconds // 60
            seconds = duration_seconds % 60
            duration_str = f"<b>{minutes} хв. {seconds} сек.</b>"
        else:
            duration_str = "невідомо"
        
        try:
            await message_bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔔 <b>Сесію завершено автоматично з боку станції!</b>\n\n"
                    f"🏁 Порт: <code>{conn_id}</code>\n"
                    f"⏱ Тривалість сесії: {duration_str}\n"
                    f"🔋 Спожито за сесію: <b>{consumed_kwh} кВт·год</b>\n"
                    f"💳 Ваш загальний баланс: <b>{balance_kwh:.2f} кВт·год</b>\n\n"
                    f"Повертаємось до головного меню мережі eVolt UA:"
                ),
                parse_mode="HTML",
                reply_markup=main_reply_menu
            )
        except Exception as e:
            logging.error(f"Помилка надсилання сповіщення: {e}")

# --- ХЕНДЛЕР 2: Клік на роз'єм ---
@charge_router.callback_query(ConnectorCallback.filter())
async def handle_connector_selection(call: CallbackQuery, callback_data: ConnectorCallback, state: FSMContext):
    id_connector = callback_data.id_connector
    await state.set_state(ChargingStates.charging_active)
    
    await state.update_data(active_connector_id=id_connector, started_at=time.time())
    await call.answer("Автентифікація сесії...")
    
    await call.message.edit_text(f"🔋 <b>Зарядка успешно активована!</b>\n🔌 Порт ID: {id_connector}\n\n<i>Кіловат-години будуть списані в кінці сесії.</i>", parse_mode="HTML")
    await call.message.answer(text="🎛️ Пульт керування:", reply_markup=charging_reply_menu)
    asyncio.create_task(simulate_station_auto_stop(chat_id=call.from_user.id, message_bot=call.bot, target_state=state, conn_id=id_connector))

# --- ХЕНДЛЕР 3: Ручна зупинка водієм ---
@charge_router.message(ChargingStates.charging_active, F.text == "🛑 Stop Charging")
@charge_router.message(ChargingStates.charging_active, Command("stop"))
async def handle_stop_charging(message: Message, state: FSMContext):
    user_data = await state.get_data()
    connector_id = user_data.get("active_connector_id", "Невідомий")
    started_at = user_data.get("started_at")
    
    await state.clear()
    
    consumed_kwh = 2.0
    await update_user_balance(message.from_user.id, consumed_kwh, t_type="charge_manual_stop")
    balance_kwh, _ = await get_user_data(message.from_user.id)
    
    if started_at:
        duration_seconds = int(time.time() - started_at)
        minutes = duration_seconds // 60
        seconds = duration_seconds % 60
        duration_str = f"<b>{minutes} хв. {seconds} сек.</b>"
    else:
        duration_str = "невідомо"
    
    await message.answer(
        f"🛑 <b>Зарядку зупинено водієм!</b>\n\n"
        f"🏁 Порт: <code>{connector_id}</code>\n"
        f"⏱ <b>Тривалість сесії:</b> {duration_str}\n"
        f"🔋 Спожито за сесію: <b>{consumed_kwh} кВт·год</b>\n"
        f"💳 Ваш загальний баланс: <b>{balance_kwh:.2f} кВт·год</b>\n\n"
        f"Повертаємось до головного меню мережі eVolt UA:",
        parse_mode="HTML",
        reply_markup=main_reply_menu
    )

# --- ХЕНДЛЕР 4: Статус ---
@charge_router.message(ChargingStates.charging_active, F.text == "📊 Status")
@charge_router.message(ChargingStates.charging_active, Command("status"))
async def command_status_charging(message: Message, state: FSMContext):
    user_data = await state.get_data()
    started_at = user_data.get("started_at")
    
    if started_at:
        current_duration = int(time.time() - started_at)
        mins = current_duration // 60
        secs = current_duration % 60
        time_str = f"{mins} хв. {secs} сек."
    else:
        time_str = "невідомо"

    await message.answer(
        f"⏳ <b>Автомобіль заряджається!</b>\n"
        f"🔌 Порт: <code>{user_data.get('active_connector_id')}</code>\n"
        f"⏱ Поточний час у сесії: <b>{time_str}</b>", 
        parse_mode="HTML"
    )

# --- ХЕНДЛЕР 5: Заглушка ---
@charge_router.message(ChargingStates.charging_active)
async def process_text_during_charge(message: Message):
    await message.answer("🚨 <b>Йде зарядка!</b> Використовуйте нижні кнопки керування.", parse_mode="HTML")
