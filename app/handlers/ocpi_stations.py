import logging
import asyncio
from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

# Імпортуємо інструменти для роботи з твоєю базою даних
import app.database.connection as db_conn
from app.database.connection import get_user_data

router = Router()
logger = logging.getLogger(__name__)

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
            
            # 3. Зчитуємо конектори та їх статус
            connectors = await conn.fetch("""
                SELECT c.standard, c.power_type, e.status 
                FROM ocpi_connectors c
                JOIN ocpi_evses e ON c.evse_uid = e.uid
                WHERE e.location_id = $1
            """, location_id)
            
            conn_list = [{"standard": r['standard'], "power_type": r['power_type'], "status": r['status']} for r in connectors]
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


@router.callback_query(F.data.startswith("ocpi_start_"), StateFilter("*"))
async def handle_start_charging(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    # Розпаковуємо дані з кнопки (формат: ocpi_start_ID:Порт:Вартість)
    payload = callback.data.split(":")
    station_id = payload[0].split("_")[-1]
    connector_name = payload[1] if len(payload) > 1 else "Type 2"
    cost_kwh = float(payload[2]) if len(payload) > 2 else 5.0
    
    await callback.answer("Авторизація сесії... 🔌")
    
    # Імітуємо підключення до заліза, як у твоєму оригінальному коді
    status_msg = await callback.message.answer(f"⏳ Авторизація сесії... Підключення до порту `{connector_name}`...")
    await asyncio.sleep(1.5)
    await status_msg.delete()

    try:
        # --- ТВОЯ РЕАЛЬНА ТРАНЗАКЦІЯ В ПОСТГРЕС ---
        async with db_conn.db_pool.acquire() as conn:
            async with conn.transaction():
                # 1. Знімаємо кВт·години з балансу
                await conn.execute(
                    "UPDATE users SET balance = CAST(balance AS NUMERIC) - CAST($1 AS NUMERIC) WHERE user_id = $2", 
                    cost_kwh, user_id
                )
                # 2. Фіксуємо витрату в Ledger історії операцій
                await conn.execute("""
                    INSERT INTO kw_transactions (user_id, type, amount, description)
                    VALUES ($1, 'withdrawal', $2, $3)
                """, user_id, cost_kwh, f"Списання запуск зарядки (Порт: {connector_name})")
                
        # Зчитуємо чистий баланс прямо з бази для відображення
        final_balance, _ = await get_user_data(user_id)
        
    except Exception as e:
        logger.error(f"Database error during session start: {e}")
        await callback.message.answer("❌ Сталася помилка бази даних при запуску сесії.")
        return

    # Виводимо екран активної зарядки
    text = (
        f"⚡ **ЗАРЯДНУ СЕСІЮ РОЗПОЧАТО!**\n\n"
        f"🏢 **Станція:** ⚡ Мережа eVolt UA\n"
        f"🔌 **Активний роз'єм:** `{connector_name}`\n"
        f"🔋 **Поточний статус:** 🔵 CHARGING (ЗАРЯДЖАЄТЬСЯ)\n"
        f"📉 **Списано за старт:** {cost_kwh:.2f} кВт·год\n"
        f"💰 **Залишок на балансі:** {final_balance:.2f} кВт·год\n\n"
        f"🤖 _Контролер успішно запустив реле подачі струму._"
    )
    
    charging_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Зупинити зарядку", callback_data=f"ocpi_stop_{station_id}:{connector_name}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=charging_keyboard)

# =====================================================================
# ХЕНДЛЕР ЗУПИНКИ ЗАРЯДКИ
# =====================================================================
@router.callback_query(F.data.startswith("ocpi_stop_"), StateFilter("*"))
async def handle_stop_charging(callback: CallbackQuery):
    user_id = callback.from_user.id
    payload = callback.data.split(":")
    connector_name = payload[1] if len(payload) > 1 else "Type 2"
    
    real_balance, _ = await get_user_data(user_id)
    
    text = (
        f"🏁 **ЗАРЯДНУ СЕСІЮ УСПІШНО ЗАВЕРШЕНО!**\n\n"
        f"🛑 **Зарядку порту `{connector_name}` зупинено водієм.**\n"
        f"⏱️ **Тривалість сесії:** 0 г. 1 хв.\n"
        f"💰 **Твій фінальний баланс в базі:** {real_balance:.2f} кВт·год\n\n"
        f"Дякуємо, що заряджаєтесь в мережі eVolt UA!"
    )
    
    from app.keyboards.reply import get_main_menu
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=get_main_menu())
    await callback.answer("Зарядку успішно зупинено!")
