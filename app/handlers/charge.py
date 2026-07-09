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
# 💥 Імпортуємо функції роботи з PostgreSQL
from app.database.connection import (
    get_user_data, update_user_balance, PRICE_PER_KWH, kwh_to_uah
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


# ХЕНДЛЕР 1: Обробка введення ID станції
@charge_router.message(F.text.regexp(r"OCM-\d+"))
async def handle_station_id(message: Message):
    station_id = message.text.strip()
    
    # 💥 Перевіряємо баланс водія перед тим, як дозволити зарядку
    balance, discount = await get_user_data(message.from_user.id)
    if balance <= 0:
        await message.answer(
            f"❌ <b>Запуск заблоковано!</b>\n\n"
            f"💰 Ваш баланс: <code>{balance} грн</code>.\n"
            f"Для активації станції, будь ласка, поповніть рахунок у головному меню.",
            parse_mode="HTML"
        )
        return

    mock_api_response = [
        {"id": "4501", "type": "CCS (Type 2)", "power": "240"},
        {"id": "4502", "type": "CCS (Type 2)", "power": "240"}
    ]
    
    buttons = []
    for conn in mock_api_response:
        display_text = f"🔌 Увімкнути {conn['type']} ({conn['power']} кВт)"
        buttons.append([
            InlineKeyboardButton(
                text=display_text,
                callback_data=ConnectorCallback(station_id=station_id, id_connector=str(conn['id']), connector_type=conn['type']).pack()
            )
        ])
        
    await message.answer(
        text=f"⚡ <b>Станція ID: {station_id}</b>\n\nОберіть кабель для початку зарядки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


# ФОНОВА ТАСКА: Списання грошей після 15 секунд зарядки
async def simulate_station_auto_stop(chat_id: int, message_bot, target_state: FSMContext, conn_id: str):
    await asyncio.sleep(15)  # Симулюємо сесію
    
    current_state = await target_state.get_state()
    if current_state == ChargingStates.charging_active:
        await target_state.clear()
        
        # 💥 СИМУЛЯЦІЯ СПОЖИВАННЯ: припустимо, машина встигла "залити" 8.5 кВт·год
        consumed_kwh = 8.5
        cost_uah = consumed_kwh * PRICE_PER_KWH  # 8.5 * 15 = 127.50 грн
        
        # Списуємо гроші з PostgreSQL (передаємо мінусове значення)
        await update_user_balance(chat_id, -cost_uah, t_type="charge_session")
        
        # Беремо свіжий баланс водія після списання
        new_balance, _ = await get_user_data(chat_id)
        
        try:
            await message_bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔔 <b>Сесію завершено автоматично з боку станції!</b>\n\n"
                    f"🏁 Порт: <code>{conn_id}</code>\n"
                    f"🔋 Спожито: <code>{consumed_kwh} кВт·год</code>\n"
                    f"📉 Списано: <code>{cost_uah} грн</code> (Тариф: {PRICE_PER_KWH} грн/кВт)\n\n"
                    f"💰 Ваш залишок на рахунку: <b>{new_balance} грн</b>"
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
        f"🔋 <b>Зарядка успішно активована!</b>\n🔌 Порт ID: {id_connector}\n\n<i>Гроші будуть списані автоматично в кінці сесії.</i>",
        parse_mode="HTML"
    )
    await call.message.answer(text="🎛️ Пульт керування:", reply_markup=charging_reply_menu)
    
    asyncio.create_task(simulate_station_auto_stop(chat_id=call.from_user.id, message_bot=call.bot, target_state=state, conn_id=id_connector))


# ХЕНДЛЕР 3: Ручна зупинка
@charge_router.message(ChargingStates.charging_active, F.text == "🛑 Зупинити зарядку")
@charge_router.message(ChargingStates.charging_active, Command("stop"))
async def handle_stop_charging(message: Message, state: FSMContext):
    user_data = await state.get_data()
    connector_id = user_data.get("active_connector_id", "Невідомий")
    await state.clear()
    
    # При ручній зупинці на 5-й секунді порахуємо менше споживання, наприклад 2.0 кВт·год
    consumed_kwh = 2.0
    cost_uah = consumed_kwh * PRICE_PER_KWH  # 30 грн
    
    await update_user_balance(message.from_user.id, -cost_uah, t_type="charge_manual_stop")
    new_balance, _ = await get_user_data(message.from_user.id)
    
    await message.answer(
        f"🛑 <b>Зарядку зупинено водієм!</b>\n\n"
        f"🔋 Спожито: <code>{consumed_kwh} кВт·год</code>\n"
        f"📉 Списано: <code>{cost_uah} грн</code>\n"
        f"💰 Поточний баланс: <b>{new_balance} грн</b>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


# ХЕНДЛЕР 4: Статус
@charge_router.message(ChargingStates.charging_active, F.text == "📊 Статус")
@charge_router.message(ChargingStates.charging_active, Command("status"))
async def command_status_charging(message: Message, state: FSMContext):
    user_data = await state.get_data()
    await message.answer(f"⏳ <b>Автомобіль заряджається!</b>\n🔌 Порт: <code>{user_data.get('active_connector_id')}</code>", parse_mode="HTML")


# ХЕНДЛЕР 5: Заглушка
@charge_router.message(ChargingStates.charging_active)
async def process_text_during_charge(message: Message):
    await message.answer("🚨 <b>Йде зарядка!</b> Використовуйте нижні кнопки керування.", parse_mode="HTML")