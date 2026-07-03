import os
import asyncio
import logging
import sqlite3
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# Імпортуємо клієнт Gemini
from google import genai
from google.genai import types as genai_types
import httpx

# Завантажуємо змінні оточення
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
OCM_KEY = os.getenv("OCM_KEY")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_KEY)

class BotStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_station_id = State()

# --- Локальна База Даних ---
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

def save_station_to_local_db(station_id, name, address):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO stations (station_id, name, address) VALUES (?, ?, ?)', (station_id, name, address))
    conn.commit()
    conn.close()

def get_station_by_id(station_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT name, address FROM stations WHERE station_id = ?', (station_id,))
    result = cursor.fetchone()
    conn.close()
    return result

# --- Живий запит до API Open Charge Map ---
async def find_nearest_station(user_lat, user_lon):
    url = "https://api.openchargemap.io/v3/poi/"
    params = {
        "output": "json", 
        "latitude": user_lat, 
        "longitude": user_lon, 
        "distance": 15, 
        "distanceunit": "KM", 
        "maxresults": 10,  # Повертаємо топ-10 для точного вибору найближчої
        "key": OCM_KEY
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=5.0)
            if response.status_code != 200 or not response.json(): 
                return None
            
            poi = response.json()[0]
            addr = poi.get("AddressInfo", {})
            st_id = f"OCM-{poi.get('ID')}"
            name = addr.get("Title", "Без назви")
            address = addr.get("AddressLine1", "Адреса не вказана")
            distance = addr.get("Distance", 0.0)
            st_lat, st_lon = addr.get("Latitude"), addr.get("Longitude")
            
            # Гарне форматування роз'ємів
            conn_list = []
            for c in poi.get("Connections", []):
                title = c.get("ConnectionType", {}).get("Title", "Невідомий роз'єм")
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
            
            save_station_to_local_db(st_id, name, address)
            return (st_id, name, address, distance, connectors_text, st_lat, st_lon)
    except Exception as e:
        logging.error(f"Помилка OCM API: {e}")
        return None

# --- Меню та Клавіатури ---
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

def get_navigation_keyboard(lat, lon):
    builder = InlineKeyboardBuilder()
    builder.button(text="🗺 Google Maps", url=f"https://www.google.com/maps/search/?api=1&query={lat},{lon}")
    builder.button(text="🚙 Waze", url=f"https://waze.com/ul?ll={lat},{lon}&navigate=yes")
    builder.adjust(2)
    return builder.as_markup()

# --- Стандартні текстові та командні обробники ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, balance, discount) VALUES (?, 0.0, 1.0)', (message.from_user.id,))
    conn.commit(); conn.close()
    await message.answer(f"Доброго дня, {message.from_user.first_name}! Оберіть розділ меню:", reply_markup=get_main_menu())

@dp.message(F.text == "⬅️ Головне меню")
async def cmd_back_to_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Повертаємось до головного меню мережі eVolt UA:", reply_markup=get_main_menu())

@dp.message(F.text == "Зарядка ⚡")
async def process_charge_click(message: types.Message):
    await message.answer(
        "🔌 **Оберіть спосіб пошуку станції:**\n\n"
        "• Надішліть геопозицію, і бот знайде найближчу реальну станцію з бази Open Charge Map.\n"
        "• Або введіть ID вручну, якщо ви вже стоїте біля комплексу.",
        reply_markup=get_charge_menu()
    )

@dp.message(F.text == "⌨️ Ввести ID вручну")
async def manual_id_entry(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_station_id)
    await message.answer("Введіть ID зарядної станції (наприклад: `OCM-307584`):")

@dp.message(F.text == "Ваучер 🧾")
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

@dp.message(F.text == "Як працює? 🙄")
async def process_help_click(message: types.Message):
    await message.answer("ℹ️ **Інструкція мережі eVolt UA:**\n1. Підключіть кабель до авто.\n2. Знайдіть станцію по GPS або введіть її ID.\n3. Надішліть ID у чат для старту імітації зарядки.")

@dp.message(F.text == "Online підтримка 📢")
async def process_support_click(message: types.Message):
    await message.answer("📢 Зв'язок з оператором підтримки eVolt UA: @your_support_username")

# --- Обробка Геолокації ---
@dp.message(F.location)
async def handle_location(message: types.Message, state: FSMContext):
    await message.answer("🔍 **Звертаємось до бази даних Open Charge Map через інтернет... Обчислюємо відстань...**")
    data = await find_nearest_station(message.location.latitude, message.location.longitude)
    
    if data:
        st_id, name, addr, distance, conns, lat, lon = data
        await state.set_state(BotStates.waiting_for_station_id)
        await message.answer(
            f"📍 **Знайдено найближчу реальну станцію!**\n\n"
            f"• **Назва:** `{name}`\n"
            f"• **Адреса:** {addr}\n"
            f"• **Відстань до неї:** **{distance:.2f} км**\n"
            f"• **Доступні роз'єми:** {conns}\n\n"
            f"👉 Щоб розпочати сесію, надішліть у чат її ідентифікатор: `{st_id}`",
            parse_mode="Markdown",
            reply_markup=get_navigation_keyboard(lat, lon)
        )
    else:
        await message.answer("❌ Станцій поблизу не знайдено в Open Charge Map.")

# --- Мультимодальна Обробка Голосу ---
@dp.message(F.voice)
async def handle_voice(message: types.Message, state: FSMContext):
    await bot.send_chat_action(chat_id=message.chat.id, action="record_voice")
    ogg_path = f"v_{message.from_user.id}.ogg"
    
    try:
        voice_file = await bot.get_file(message.voice.file_id)
        await bot.download_file(voice_file.file_path, destination=ogg_path)
        
        await message.answer("🎧 *Розпізнаю ваш голос через ШІ...*")
        with open(ogg_path, "rb") as f:
            resp = ai_client.models.generate_content(
                model='gemini-3.5-flash',
                contents=[
                    genai_types.Part.from_bytes(data=f.read(), mime_type='audio/ogg'), 
                    "Перетвори це аудіо повідомлення на текст. Виведи ТІЛЬКИ розпізнаний текст українською мовою, без жодних коментарів чи додаткових знаків."
                ]
            )
        
        recognized_text = resp.text.strip() if resp.text else ""
        if not recognized_text:
            await message.answer("❌ Не вдалося розібрати слова. Спробуйте сказати чіткіше.")
            return
            
        await message.answer(f"🗣 *Ви сказали:* «{recognized_text}»")
        message.text = recognized_text
        
        # Маршрутизація розпізнаного тексту по кнопках меню
        clean_text = recognized_text.lower().replace("⚡", "").replace("🙄", "").replace("🧾", "").replace("📢", "").strip()
        if "зарядка" in clean_text:
            await process_charge_click(message)
        elif "ваучер" in clean_text:
            await process_voucher_click(message, state)
        elif "головне меню" in clean_text or "меню" in clean_text:
            await cmd_back_to_menu(message, state)
        elif "як працює" in clean_text:
            await process_help_click(message)
        elif "підтримка" in clean_text or "сапорт" in clean_text:
            await process_support_click(message)
        else:
            await handle_ai_chat(message)
    except Exception as e:
        logging.error(f"Помилка голосу: {e}")
        await message.answer("🤖 Ой, не вдалося розпізнати аудіо. Напишіть, будь ласка, текстом.")
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

# --- Інші стани (Ваучери та Сесії) ---
@dp.callback_query(lambda c: c.data.startswith('buy_pack_') or c.data == 'activate_night')
async def process_tariff_purchase(callback_query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback_query.from_user.id
    action = callback_query.data
    if action == "buy_pack_50":
        update_user_balance(user_id, 750.0)
        await callback_query.message.answer("✅ Пакет 50 кВт·год активовано. Рахунок поповнено!", reply_markup=get_main_menu())
    elif action == "buy_pack_100":
        update_user_balance(user_id, 1350.0)
        await callback_query.message.answer("🔥 Пакет 100 кВт·год активовано. Рахунок поповнено!", reply_markup=get_main_menu())
    elif action == "activate_night":
        set_user_discount(user_id, 0.85)
        await callback_query.message.answer("🌙 Нічний безліміт підключено (Знижка 15%).", reply_markup=get_main_menu())
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
        charge_cost = 45.0 * discount
        
        await message.answer(f"⏳ Авторизація на станції `{name}`...")
        await asyncio.sleep(1.5)
        
        update_user_balance(message.from_user.id, -charge_cost)
        new_balance, _ = get_user_data(message.from_user.id)
        await message.answer(
            f"✅ **Зарядку успішно завершено!**\n\n"
            f"• Комплекс: {name}\n"
            f"• Списано: {charge_cost:.2f} грн\n"
            f"💰 **Залишок на рахунку:** {new_balance:.2f} грн.",
            reply_markup=get_main_menu()
        )
    else:
        await message.answer("❌ Станцію з таким ID не знайдено в базі.", reply_markup=get_main_menu())

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

# --- Внутрішній ШІ-чат ---
@dp.message(lambda m: m.text and not m.text.startswith('/'))
async def handle_ai_chat(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    system_instruction = (
        "Ти — інтелектуальний ШІ-асистент мережі зарядних станцій eVolt UA. "
        "Твоє завдання — максимально корисно відповідати водіям електромобілів. "
        "Якщо користувач запитує про конкретну локацію (наприклад, чи є зарядка в Зубрі тощо), "
        "використовуй свої знання та детально розпиши, які станції чи відомі комплекси там є. "
        "Наприкінці відповіді завжди додавай, що для пошуку точних станцій мережі в реальному часі "
        "найкраще скористатися кнопкою 'Зарядка ⚡' та надіслати свою геолокацію."
    )
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.5-flash',
            contents=message.text,
            config={'system_instruction': system_instruction}
        )
        await message.answer(response.text)
    except:
        await message.answer("🤖 Мій ШІ-модуль зараз оновлюється. Спробуйте скористатися кнопками меню!")

# --- Старт проєкту ---
async def main():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, discount REAL DEFAULT 1.0)')
    cursor.execute('CREATE TABLE IF NOT EXISTS stations (station_id TEXT PRIMARY KEY, name TEXT, address TEXT)')
    conn.commit(); conn.close()
    
    print("Database is checked. Bot is running flawlessly...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())