import os
import asyncio
import logging
import sqlite3
import math
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# Імпортуємо офіційний клієнт Gemini та httpx для живих запитів
from google import genai
import httpx

# Завантажуємо змінні з файлу .env
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
OCM_KEY = os.getenv("OCM_KEY")  # Отримуємо ключ Open Charge Map

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Ініціалізуємо клієнт Gemini
ai_client = genai.Client(api_key=GEMINI_KEY)

class BotStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_station_id = State()

def get_user_data(user_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT balance, discount FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result if result else (0.0, 1.0)

def update_user_balance(user_id, amount):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def set_user_discount(user_id, discount_value):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET discount = ? WHERE user_id = ?', (discount_value, user_id))
    conn.commit()
    conn.close()

# Допоміжна функція: зберігає/оновлює станцію в локальній базі
def save_station_to_local_db(station_id, name, address):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO stations (station_id, name, address, lat, lon) 
        VALUES (?, ?, ?, 0.0, 0.0)
    ''', (station_id, name, address))
    conn.commit()
    conn.close()

def get_station_by_id(station_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT name, address FROM stations WHERE station_id = ?', (station_id,))
    result = cursor.fetchone()
    conn.close()
    return result

# ЖИВИЙ ЗАПИТ З КООРДИНАТАМИ ДЛЯ НАВІГАЦІЇ
async def find_nearest_station(user_lat, user_lon):
    url = "https://api.openchargemap.io/v3/poi/"
    params = {
        "output": "json",
        "latitude": user_lat,
        "longitude": user_lon,
        "distance": 15,          
        "distanceunit": "KM",
        "maxresults": 10,        
        "key": OCM_KEY
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=5.0)
            if response.status_code != 200:
                logging.error(f"OCM API Error: Status {response.status_code}")
                return None
            
            data = response.json()
            if not data:
                return None
            
            nearest_poi = data[0]
            
            address_info = nearest_poi.get("AddressInfo", {})
            raw_id = nearest_poi.get("ID")
            station_id = f"OCM-{raw_id}"
            name = address_info.get("Title", "Без назви")
            address = address_info.get("AddressLine1", "Адреса не вказана")
            distance = address_info.get("Distance", 0.0)
            
            # Витягуємо точні координати станції з OCM для карт
            st_lat = address_info.get("Latitude")
            st_lon = address_info.get("Longitude")
            
            connections = nearest_poi.get("Connections", [])
            conn_list = []
            for c in connections:
                conn_type = c.get("ConnectionType", {})
                title = conn_type.get("Title", "Невідомий роз'єм")
                title = title.replace(" (Socket Only)", "").replace(" (Tethered Cable)", "")
                
                power = c.get("PowerKW")
                quantity = c.get("Quantity")
                
                info_str = title
                if power:
                    info_str += f" ({power} кВт)"
                if quantity and int(quantity) > 1:
                    info_str += f" x{quantity}"
                
                if info_str not in conn_list:
                    conn_list.append(info_str)
            
            connectors_text = ", ".join(conn_list) if conn_list else "Інформація відсутня"
            
            save_station_to_local_db(station_id, name, address)
            
            return (station_id, name, address, distance, connectors_text, st_lat, st_lon)
            
    except Exception as e:
        logging.error(f"Помилка під час запиту до Open Charge Map API: {e}")
        return None

def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="Зарядка ⚡")
    builder.button(text="Як працює? 🙄")
    builder.button(text="Ваучер 🧾")
    builder.button(text="Online підтримка 📢")
    builder.adjust(1, 1, 2)
    return builder.as_markup(resize_keyboard=True)

def get_charge_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📍 Надіслати розташування", request_location=True)
    builder.button(text="⌨️ Ввести ID вручну")
    builder.button(text="⬅️ Головне меню")
    builder.adjust(1, 1, 1)
    return builder.as_markup(resize_keyboard=True)

def get_tariffs_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔋 Пакет 50 кВт·год (750 грн)", callback_data="buy_pack_50")
    builder.button(text="🔥 Пакет 100 кВт·год (1350 грн) -10%", callback_data="buy_pack_100")
    builder.button(text="🌙 Активувати Нічний Безліміт", callback_data="activate_night")
    builder.adjust(1, 1, 1)
    return builder.as_markup()

# Функція генерації кнопок навігації
def get_navigation_keyboard(lat, lon):
    builder = InlineKeyboardBuilder()
    builder.button(text="🗺 Google Maps", url=f"https://www.google.com/maps/search/?api=1&query={lat},{lon}")
    builder.button(text="🚙 Waze", url=f"https://waze.com/ul?ll={lat},{lon}&navigate=yes")
    builder.adjust(2) # Розташує дві кнопки в один ряд
    return builder.as_markup()

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username, first_name, balance, discount) VALUES (?, ?, ?, 0.0, 1.0)',
                   (message.from_user.id, message.from_user.username, message.from_user.first_name))
    conn.commit()
    conn.close()
    await message.answer(f"Доброго дня, {message.from_user.first_name}! Оберіть розділ:", reply_markup=get_main_menu())

@dp.message(lambda message: message.text == "⬅️ Головне меню")
async def cmd_back_to_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Повертаємось до головного меню:", reply_markup=get_main_menu())

@dp.message(lambda message: message.text == "Зарядка ⚡")
async def process_charge_click(message: types.Message):
    balance, _ = get_user_data(message.from_user.id)
    if balance <= 0:
        await message.answer(f"❌ **Недостатньо коштів.**\nБаланс: {balance:.2f} грн.\nПоповніть рахунок у розділі *Ваучер 🧾*.")
    else:
        await message.answer(
            "🔌 **Оберіть спосіб пошуку станції:**\n\n"
            "• Надішліть геопозицію, і бот знайде найближчу реальну станцію з бази Open Charge Map.\n"
            "• Або введіть ID вручну, якщо ви вже біля стовпчика.",
            reply_markup=get_charge_menu()
        )

@dp.message(lambda message: message.text == "⌨️ Ввести ID вручну")
async def manual_id_entry(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_station_id)
    await message.answer("Введіть ID зарядної станції (наприклад: `OCM-12345`):")

@dp.message(lambda message: message.location is not None)
async def handle_location(message: types.Message, state: FSMContext):
    lat = message.location.latitude
    lon = message.location.longitude
    
    await message.answer("🔍 **Звертаємось до бази даних Open Charge Map через інтернет... Обчислюємо відстань...**")
    
    station_data = await find_nearest_station(lat, lon)
    
    if station_data:
        station_id, name, address, distance, connectors_text, st_lat, st_lon = station_data
        await state.set_state(BotStates.waiting_for_station_id)
        
        # Додаємо створену Inline-клавіатуру навігації у reply_markup
        await message.answer(
            f"📍 **Знайдено найближчу реальну станцію!**\n\n"
            f"• **Назва:** `{name}`\n"
            f"• **Адреса:** {address}\n"
            f"• **Відстань до неї:** **{distance:.2f} км**\n"
            f"• **Доступні роз'єми:** {connectors_text}\n\n"
            f"👉 Щоб розпочати сесію, надішліть у чат її ідентифікатор: `{station_id}`",
            parse_mode="Markdown",
            reply_markup=get_navigation_keyboard(st_lat, st_lon)
        )
    else:
        await message.answer("❌ Станцій поблизу не знайдено в Open Charge Map або виникла помилка зв'язку.")

@dp.message(lambda message: message.text == "Ваучер 🧾")
async def process_voucher_click(message: types.Message, state: FSMContext):
    balance, discount = get_user_data(message.from_user.id)
    status_discount = "Немає" if discount == 1.0 else "Активовано знижку 15% 🔥"
    await message.answer(
        f"💳 **Ваш баланс:** {balance:.2f} грн.\n"
        f"📉 **Статус знижки:** {status_discount}\n\n"
        f"🎁 Оберіть тарифний пакет або введіть код ваучера руками:", 
        reply_markup=get_tariffs_keyboard()
    )
    await state.set_state(BotStates.waiting_for_code)

@dp.callback_query(lambda c: c.data.startswith('buy_pack_') or c.data == 'activate_night')
async def process_tariff_purchase(callback_query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback_query.from_user.id
    action = callback_query.data
    
    if action == "buy_pack_50":
        update_user_balance(user_id, 750.0)
        await callback_query.message.answer("✅ Пакет 50 кВт·год активовано. Зараховано **750.00 грн**.", reply_markup=get_main_menu())
    elif action == "buy_pack_100":
        update_user_balance(user_id, 1350.0)
        await callback_query.message.answer("🔥 Пакет 100 кВт·год активовано. Зараховано **1350.00 грн**.", reply_markup=get_main_menu())
    elif action == "activate_night":
        set_user_discount(user_id, 0.85)
        await callback_query.message.answer("🌙 Нічний безліміт підключено (Знижка 15% на сесії).", reply_markup=get_main_menu())
    await callback_query.answer()

@dp.message(StateFilter(BotStates.waiting_for_station_id))
async def process_station_id(message: types.Message, state: FSMContext):
    station_id = message.text.strip().upper()
    
    if not station_id.startswith("OCM-"):
        await state.clear()
        await handle_ai_chat(message)
        return

    await state.clear()
    station_info = get_station_by_id(station_id)
    
    if station_info:
        name, address = station_info
        balance, discount = get_user_data(message.from_user.id)
        base_cost = 45.0
        charge_cost = base_cost * discount
        
        await message.answer(f"⏳ Авторизація на станції `{name}` ({address})...")
        await asyncio.sleep(1.5)
        
        update_user_balance(message.from_user.id, -charge_cost)
        new_balance, _ = get_user_data(message.from_user.id)
        
        await message.answer(
            f"✅ **Зарядку успішно завершено!**\n\n"
            f"📋 **Звіт по сесії:**\n"
            f"• Комплекс: {name}\n"
            f"• ID станції: {station_id}\n"
            f"• Списано: {charge_cost:.2f} грн\n\n"
            f"💰 **Залишок на рахунку:** {new_balance:.2f} грн.",
            reply_markup=get_main_menu()
        )
    else:
        await message.answer("❌ **Станцію з таким ID не знайдено в базі.**", reply_markup=get_main_menu())

@dp.message(StateFilter(BotStates.waiting_for_code))
async def process_text_voucher(message: types.Message, state: FSMContext):
    user_code = message.text.strip()
    await state.clear()
    if user_code == "VOLT100":
        update_user_balance(message.from_user.id, 100.0)
        balance, _ = get_user_data(message.from_user.id)
        await message.answer(f"✅ Код прийнято! Баланс: {balance:.2f} грн.", reply_markup=get_main_menu())
    else:
        await message.answer("❌ Невірний код ваучера.", reply_markup=get_main_menu())

@dp.message(lambda message: message.text == "Як працює? 🙄")
async def process_help_click(message: types.Message):
    await message.answer("ℹ️ Інструкція: підключіть кабель, знайдіть станцію по GPS або введіть її ID, почніть сесію.")

@dp.message(lambda message: message.text == "Online підтримка 📢")
async def process_support_click(message: types.Message):
    await message.answer("Сапорт: @your_support_username")

@dp.message(lambda message: message.text and not message.text.startswith('/'))
async def handle_ai_chat(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    system_instruction = (
        "Ти — інтелектуальний ШІ-асистент мережі зарядних станцій eVolt UA. "
        "Твоє завдання — максимально корисно відповідати водіям електромобілів. "
        "1. Якщо користувач запитує про конкретну локацію (наприклад, 'чи є зарядка в Зубрі', 'де зарядитися поруч з OKKO' тощо), "
        "використовуй свої знання про світ і детально розпиши, які станції, роз'єми (наприклад, GB/T, Type 2) чи відомі комплекси там є. "
        "2. Наприкінці своєї відповіді завжди ввічливо додавай, що для пошуку та запуску точних станцій нашої мережі в реальному часі "
        "найкраще скористатися кнопкою 'Зарядка ⚡' та надіслати свою геолокацію."
    )
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.5-flash',
            contents=message.text,
            config={'system_instruction': system_instruction}
        )
        await message.answer(response.text)
    except Exception as e:
        logging.error(f"Помилка ШІ: {e}")
        await message.answer("🤖 Ой, мій ШІ-модуль зараз перезавантажується. Спробуйте скористатися кнопками меню!")

async def main():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0.0,
            discount REAL DEFAULT 1.0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stations (
            station_id TEXT PRIMARY KEY,
            name TEXT,
            address TEXT,
            lat REAL,
            lon REAL
        )
    ''')
    conn.commit()
    conn.close()
    
    print("Database is ready. Bot is running with Gemini 3.5 AI and LIVE Navigation POI...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())