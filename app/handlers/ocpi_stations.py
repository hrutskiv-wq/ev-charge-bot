import logging
import asyncio
from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

# Імпортуємо інструменти для роботи з PostgreSQL та сервіс команд
import app.database.connection as db_conn
from app.services.ocpi.commands_service import OCPICommandsService
from app.services.ocpi.config import OCPIConfig

# Ініціалізуємо роутер та логгер
router = Router()
logger = logging.getLogger(__name__)

commands_service = OCPICommandsService()

async def get_user_balance(user_id: int) -> float:
    """
    Повертає баланс користувача (кВт·год).

    Раніше тут окремим запитом рахувалась SUM(kw_transactions.amount) —
    третє незалежне джерело балансу в кодовій базі (на додачу до
    users.balance в get_user_data і до перевірки в OCPICommandsService),
    яке з часом розходилось з тим, що бачив користувач в /start. Тепер усі
    три місця читають те саме кешоване users.balance.
    """
    try:
        balance, _ = await db_conn.get_user_data(user_id)
        return float(balance)
    except Exception as e:
        logger.error(f"Помилка отримання балансу для {user_id}: {e}")
        return 0.0

async def get_station_data_from_db(location_id: str = "LOC-001"):
    """Отримує повні дані про локацію, конектори та тариф безпосередньо з PostgreSQL"""
    try:
        pool = await db_conn.get_db_pool()
        async with pool.acquire() as conn:
            # 1. Зчитуємо саму локацію
            loc = await conn.fetchrow("SELECT name, address, city FROM ocpi_locations WHERE id = $1", location_id)
            if not loc:
                return None
                
            # 2. Зчитуємо тариф
            tariff = await conn.fetchrow("SELECT price, currency FROM ocpi_tariffs LIMIT 1")
            price = float(tariff['price']) if tariff else 12.50
            currency = tariff['currency'] if tariff else "UAH"
            
            # 3. Зчитуємо конектори (включаючи ID та UID для OCPI)
            connectors = await conn.fetch("""
                SELECT c.id as connector_id, c.standard, c.power_type, e.uid as evse_uid, e.status 
                FROM ocpi_connectors c
                JOIN ocpi_evses e ON c.evse_uid = e.uid
                WHERE e.location_id = $1
            """, location_id)
            
            conn_list = [
                {
                    "connector_id": r['connector_id'],
                    "standard": r['standard'],
                    "power_type": r['power_type'],
                    "evse_uid": r['evse_uid'],
                    "status": r['status']
                } 
                for r in connectors
            ]
            status = conn_list[0]['status'] if conn_list else "UNKNOWN"
            
            return {
                "id": location_id,
                "name": loc['name'],
                "address": f"Львівська обл., с. {loc['city']}, {loc['address']}",
                "status": status,
                "price": f"{price:.2f} {currency}/кВт·год",
                "raw_price": price,
                "connectors": conn_list
            }
    except Exception as e:
        logger.error(f"Помилка зчитування станції з БД: {e}")
        return None

@router.message(Command("ocpi"), StateFilter("*"))
async def cmd_ocpi_stations(message: Message, state: FSMContext):
    await state.clear()  
    data = await get_station_data_from_db("LOC-001")
    if not data:
        await message.answer("❌ Зарядна станція наразі оффлайн.")
        return
        
    from app.keyboards.ocpi_kb import get_station_keyboard
    text = (
        f"🏢 <b>Зарядна станція:</b> {data['name']}\n"
        f"📍 <b>Адреса:</b> {data['address']}\n"
        f"💳 <b>Вартість:</b> {data['price']}\n"
        f"🟢 <b>Поточний статус:</b> {data['status']}"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_station_keyboard(data["id"], data["connectors"], data["raw_price"]))

@router.callback_query(F.data.startswith("ocpi_refresh_"), StateFilter("*"))
async def handle_refresh(callback: CallbackQuery):
    payload = callback.data.split("_")
    station_id = payload[-1] if len(payload) > 1 else "LOC-001"
    
    data = await get_station_data_from_db(station_id)
    if not data:
        await callback.answer("❌ Помилка завантаження даних.")
        return
        
    text = (
        f"🏢 <b>Зарядна станція:</b> {data['name']}\n"
        f"📍 <b>Адреса:</b> {data['address']}\n"
        f"💳 <b>Вартість:</b> {data['price']}\n"
        f"🟢 <b>Поточний статус:</b> {data['status']}\n\n"
        f"🕒 <i>Дані оновлено з PostgreSQL мережі eVolt!</i>"
    )
    from app.keyboards.ocpi_kb import get_station_keyboard
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_station_keyboard(data["id"], data["connectors"], data["raw_price"]))
    except Exception:
        pass
    await callback.answer("Дані оновлено!")


@router.callback_query(F.data.startswith("ocpi_st_"), StateFilter("*"))
async def handle_start_charging(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    # Розпаковуємо дані з кнопки: ocpi_st_LOCATION_ID:EVSE_UID:CONNECTOR_ID
    try:
        payload = callback.data.split(":")
        location_id = payload[0].split("_")[-1]
        evse_uid = payload[1]
        connector_id = payload[2]
    except Exception as e:
        logger.error(f"Помилка парсингу callback_data: {e}")
        await callback.answer("❌ Помилка зчитування даних роз'єму.")
        return
        
    await callback.answer("Ініціалізація OCPI сесії... 🔌")
    
    config = OCPIConfig()
    base_commands_url = f"{config.CPO_BASE_URL}/ocpi/cpo/2.2.1/commands"

    # Викликаємо асинхронний сервіс старту
    response = await commands_service.initiate_start_session(
        user_id=user_id,
        location_id=location_id,
        evse_uid=evse_uid,
        connector_id=connector_id,
        base_commands_url=base_commands_url
    )

    if response["status"] != "ACCEPTED":
        await callback.message.answer(response["message"], parse_mode="HTML")
        return

    current_balance = await get_user_balance(user_id)
    test_session_id = f"session_evolt_{user_id}"

    text = (
        f"⚡ <b>ЗАПИТ НА ЗАРЯДКУ НАДІСЛАНО!</b>\n\n"
        f"🏢 <b>Станція:</b> ⚡ Мережа eVolt UA\n"
        f"🔌 <b>Контролер роз'єму:</b> `{evse_uid}` (Порт: {connector_id})\n"
        f"🔋 <b>Статус команди:</b> 🟡 ACCEPTED (ПРИЙНЯТО В ОБРОБКУ)\n"
        f"💰 <b>Твій поточний баланс:</b> {current_balance:.2f} кВт·год\n\n"
        f"🤖 <i>Зараз залізо CPO розблокує кабель. Зачекайте кілька секунд до початку заряджання...</i>"
    )
    
    charging_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Зупинити зарядку", callback_data=f"ocpi_stop_{location_id}:{test_session_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=charging_keyboard)


# =====================================================================
# ХЕНДЛЕР ЗУПИНКИ ЗАРЯДКИ
# =====================================================================
@router.callback_query(F.data.startswith("ocpi_stop_"), StateFilter("*"))
async def handle_stop_charging(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    # Формат callback_data: ocpi_stop_LOCATION_ID:SESSION_ID
    try:
        payload = callback.data.split(":")
        session_id = payload[1]
    except Exception as e:
        logger.error(f"Помилка розпарсингу даних зупинки: {e}")
        await callback.answer("❌ Помилка ідентифікації сесії.")
        return

    await callback.answer("Зупинка сесії через OCPI... 🔌")
    
    config = OCPIConfig()
    base_commands_url = f"{config.CPO_BASE_URL}/ocpi/cpo/2.2.1/commands"

    # Викликаємо асинхронний метод зупинки
    response = await commands_service.initiate_stop_session(
        user_id=user_id,
        session_id=session_id,
        base_commands_url=base_commands_url
    )

    if response["status"] != "ACCEPTED":
        await callback.message.answer(response["message"], parse_mode="HTML")
        return

    # Повідомляємо водія про успішний запит на зупинку
    text = (
        f"🛑 <b>ЗАПИТ НА ЗУПИНКУ НАДІСЛАНО!</b>\n\n"
        f"🔌 <b>Сесія ID:</b> `{session_id}`\n"
        f"⏳ <b>Статус:</b> 🟡 Очікуємо підтвердження від заліза про вимкнення реле..."
    )
    
    from app.keyboards.reply import get_main_menu
    await callback.message.answer(text, parse_mode="HTML", reply_markup=get_main_menu())
