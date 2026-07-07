import os
import asyncio
import logging
import aiosqlite
from redis.asyncio import Redis
from aiogram.fsm.storage.redis import RedisStorage
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from google import genai
from google.genai import types as genai_types
import httpx
from aiocache import cached
from aiocache.serializers import JsonSerializer

# --- Налаштування ---
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
PAYMENT_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")
OCM_KEY = os.getenv("OCM_KEY")
ADMIN_IDS = [514533557]
PRICE_PER_KWH = 9.0  # Вартість 1 кВт для конвертації

# Захист 1: Перевірка наявності критичних змінних оточення
if not API_TOKEN or not GEMINI_KEY:
    raise ValueError("❌ Критична помилка: Не знайдено ключі BOT_TOKEN або GEMINI_API_KEY у файлі .env!")

logging.basicConfig(level=logging.INFO)
# --- ІНІЦІАЛІЗАЦІЯ ---
# Видаліть всі попередні bot = ... та dp = ... і залиште тільки цей блок:

bot = Bot(token=API_TOKEN)

# 1. Створюємо підключення до Redis
redis = Redis(host='redis', port=6379, db=0)

# 2. Створюємо сховище станів (FSM) на базі Redis
storage = RedisStorage(redis=redis)

# 3. Створюємо диспетчер ОДИН РАЗ із сховищем
dp = Dispatcher(storage=storage)

# 4. Ініціалізуємо AI клієнт
ai_client = genai.Client(api_key=GEMINI_KEY)  
ai_client = genai.Client(api_key=GEMINI_KEY)

# --- Конвертери ---
def uah_to_kwh(amount_uah): return amount_uah / PRICE_PER_KWH
def kwh_to_uah(amount_kwh): return amount_kwh * PRICE_PER_KWH

class BotStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_station_id = State()
    waiting_for_connector = State()

# --- Ізольована робота з Базою Даних (Захист від Database is locked) ---
class Database:
    def __init__(self, db_path='users.db'):
        self.db_path = db_path

    async def execute_commit(self, query, params=()):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(query, params)
            await db.commit()

    async def fetchone(self, query, params=()):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, query, params=()):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                return await cursor.fetchall()

db = Database()

async def initialize_db():
    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, discount REAL DEFAULT 1.0)')
        await conn.execute('''
                CREATE TABLE IF NOT EXISTS stations (
                    station_id TEXT PRIMARY KEY, name TEXT, address TEXT, lat REAL, lon REAL, connectors TEXT
                )
            ''')
        await conn.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    type TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        await conn.commit()

async def get_user_data(user_id):
    result = await db.fetchone('SELECT balance, discount FROM users WHERE user_id = ?', (user_id,))
    return result if result else (0.0, 1.0)

async def log_transaction(user_id, amount, t_type):
    await db.execute_commit(
        'INSERT INTO transactions (user_id, amount, type) VALUES (?, ?, ?)',
        (user_id, amount, t_type)
    )

async def update_user_balance(user_id, amount_uah, t_type="deposit"):
    await db.execute_commit('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount_uah, user_id))
    await log_transaction(user_id, amount_uah, t_type)

async def admin_set_balance(user_id, amount_uah):
    await db.execute_commit('UPDATE users SET balance = ? WHERE user_id = ?', (amount_uah, user_id))
    await log_transaction(user_id, amount_uah, 'admin_set')

async def set_user_discount(user_id, discount_value):
    await db.execute_commit('UPDATE users SET discount = ? WHERE user_id = ?', (discount_value, user_id))

async def admin_adjust_balance(user_id: int, amount: float):
    await update_user_balance(user_id, amount, "admin_adjustment")

async def save_station_to_local_db(station_id, name, address, connectors, lat=0.0, lon=0.0):
    await db.execute_commit('''
        INSERT OR REPLACE INTO stations (station_id, name, address, connectors, lat, lon) 
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (station_id, name, address, connectors, lat, lon))

async def get_station_by_id(station_id):
    return await db.fetchone('SELECT name, address, connectors FROM stations WHERE station_id = ?', (station_id,))

# --- Кастомний ключ для кешування по геолокації ---
def location_key_builder(func, user_lat, user_lon, *args, **kwargs):
    rounded_lat = round(user_lat, 3)
    rounded_lon = round(user_lon, 3)
    return f"{func.__module__}{func.__name__}:{rounded_lat}:{rounded_lon}"

# --- Запит до API Open Charge Map (з обробкою таймаутів) ---
@cached(ttl=300, key_builder=location_key_builder, serializer=JsonSerializer())
async def find_three_nearest_stations(user_lat, user_lon):
    url = "https://api.openchargemap.io/v3/poi/"
    params = {
        "output": "json",
        "latitude": user_lat,
        "longitude": user_lon,
        "distance": 25,          
        "distanceunit": "KM",
        "maxresults": 3,
        "key": OCM_KEY
    }
    
    try:
        logging.info(f"Виконуємо запит до OCM API для координат: {user_lat}, {user_lon}")
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=6.0) 
            if response.status_code != 200 or not response.json():
                return None
            
            stations_list = []
            for poi in response.json():
                address_info = poi.get("AddressInfo", {})
                operator_info = poi.get("OperatorInfo", {})
                
                raw_id = poi.get("ID")
                station_id = f"OCM-{raw_id}"
                name = address_info.get("Title", "Без назви")
                address = address_info.get("AddressLine1", "Адреса не вказана")
                distance = address_info.get("Distance", 0.0)
                st_lat = address_info.get("Latitude")
                st_lon = address_info.get("Longitude")
                
                operator_name = operator_info.get("Title", "Невідомий оператор")
                if "Unknown" in operator_name or "Business" in operator_name:
                    operator_name = "Приватна/Муніципальна"
                
                connections = poi.get("Connections", [])
                conn_list = []
                for c in connections:
                    conn_type = c.get("ConnectionType", {})
                    title = conn_type.get("Title", "Невідомий роз'єм")
                    title = title.replace(" (Socket Only)", "").replace(" (Tethered Cable)", "")
                    power = c.get("PowerKW")
                    quantity = c.get("Quantity")
                    
                    info_str = title
                    if power: info_str += f" ({power} кВт)"
                    if quantity and int(quantity) > 1: info_str += f" x{quantity}"
                    if info_str not in conn_list: conn_list.append(info_str)
                
                connectors_text = "; ".join(conn_list) if conn_list else "Інформація відсутня"
                await save_station_to_local_db(station_id, name, address, connectors_text, st_lat, st_lon)
                
                stations_list.append({
                    "id": station_id,
                    "name": name,
                    "address": address,
                    "distance": distance,
                    "operator": operator_name,
                    "connectors": connectors_text.replace("; ", ", "),
                    "lat": st_lat,
                    "lon": st_lon
                })
            
            return stations_list
            
    except httpx.TimeoutException:
        logging.error("OCM API Timeout")
        return None
    except Exception as e:
        logging.error(f"Помилка OCM API: {e}")
        return None

# --- Меню та Конструктори Клавіатур ---
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

def get_single_station_keyboard(lat, lon):
    builder = InlineKeyboardBuilder()
    builder.button(text="🗺 Google Maps", url=f"https://www.google.com/maps/search/?api=1&query={lat},{lon}")
    builder.button(text="🚙 Waze", url=f"https://waze.com/ul?ll={lat},{lon}&navigate=yes")
    builder.adjust(2)
    return builder.as_markup()

def get_connectors_keyboard(connectors_string):
    builder = InlineKeyboardBuilder()
    connectors_list = connectors_string.split("; ")
    for conn in connectors_list:
        if conn.strip() and conn != "Інформація відсутня":
            builder.button(text=f"🔌 Увімкнути {conn}", callback_data=f"select_conn:{conn[:30]}")
    builder.adjust(1)
    return builder.as_markup()

# --- Обробники текстових команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await db.execute_commit('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (message.from_user.id,))
    await message.answer(f"Доброго дня, {message.from_user.first_name}! Оберіть розділ меню:", reply_markup=get_main_menu())

@dp.message(lambda m: m.text and "головне меню" in m.text.lower())
async def cmd_back_to_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Повертаємось до головного меню мережі eVolt UA:", reply_markup=get_main_menu())

@dp.message(lambda m: m.text and "зарядка" in m.text.lower())
async def process_charge_click(message: types.Message):
    balance, _ = await get_user_data(message.from_user.id)
    if balance <= 0:
        await message.answer(f"❌ **Недостатньо коштів.**\nБаланс: {balance:.2f} грн.\nПоповніть рахунок.")
    else:
        await message.answer(
            "🔌 **Оберіть спосіб пошуку станції:**\n\n"
            "• Надішліть геопозицію, і бот знайде станції.\n"
            "• Або введіть ID вручну.",
            reply_markup=get_charge_menu()
        )

@dp.message(lambda m: m.text and "ввести id" in m.text.lower())
async def manual_id_entry(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_station_id)
    await message.answer("Введіть ID зарядної станції (наприклад: `OCM-307584`):")

@dp.message(lambda m: m.text and "ваучер" in m.text.lower())
async def process_voucher_click(message: types.Message, state: FSMContext):
    balance_uah, discount = await get_user_data(message.from_user.id)
    balance_kwh = uah_to_kwh(balance_uah)
    
    await message.answer(
        f"💳 **Ваш баланс:** `{balance_kwh:.2f} кВт·год`\n\n"
        f"🎁 Оберіть тарифний пакет:",
        reply_markup=get_tariffs_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(BotStates.waiting_for_code)

@dp.message(lambda m: m.text and "як працює" in m.text.lower())
async def process_help_click(message: types.Message):
    await message.answer("ℹ️ **Інструкція мережі eVolt UA:**\n1. Підключіть кабель.\n2. Знайдіть станцію по GPS.\n3. Оберіть роз'єм у чаті для старту сесії.")

@dp.message(lambda m: m.text and "підтримка" in m.text.lower())
async def process_support_click(message: types.Message):
    await message.answer("📢 Зв'язок з оператором підтримки eVolt UA: @your_support_username")

@dp.message(Command("history"))
async def cmd_history(message: types.Message, state: FSMContext):
    await state.clear()
    rows = await db.fetchall(
        'SELECT amount, type, created_at FROM transactions WHERE user_id = ? ORDER BY transaction_id DESC LIMIT 5',
        (message.from_user.id,)
    )

    if not rows:
        await message.answer("📝 У вас ще немає історії операцій.")
        return
 
    text = "📜 **Ваші останні 5 операцій:**\n\n"
    for amt, t, date_str in rows:
        dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
        formatted_date = dt.strftime('%d.%m %H:%M')
        kwh_amount = uah_to_kwh(amt)

        type_labels = {
            "deposit": "📥 Поповнення",
            "charge": "⚡ Зарядка",
            "admin_adjustment": "🛠 Коригування",
            "admin_set": "⚙️ Встановлення"
        }
        label = type_labels.get(t, t)
        sign = "+" if amt > 0 else ""

        text += f"📅 {formatted_date} | {label}: `{sign}{kwh_amount:.2f} кВт`\n"
 
    await message.answer(text, parse_mode="Markdown")

# --- Адмін-команди ---
@dp.message(Command("add_balance"))
async def cmd_add_balance(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    args = message.text.split()
    if len(args) != 3:
        return await message.answer("❌ **Неправильний формат.**\nВикористовуйте: `/add_balance <user_id> <сума_грн>`")
    try:
        target_user_id = int(args[1])
        amount = float(args[2])
        await admin_adjust_balance(target_user_id, amount)
        await message.answer(f"✅ Баланс користувача `{target_user_id}` скориговано на `{amount}` грн.")
    except (ValueError, IndexError):
        await message.answer("❌ **Помилка.**\nПеревірте, що `user_id` та сума є числами.")

@dp.message(Command("setbalance"))
async def cmd_set_balance(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    args = message.text.split()
    if len(args) != 3:
        return await message.answer("❌ **Неправильний формат.**\nВикористання: `/setbalance <user_id> <сума_квт>`")
    try:
        target_user_id = int(args[1])
        kwh = float(args[2])
        await admin_set_balance(target_user_id, kwh_to_uah(kwh))
        await message.answer(f"✅ Баланс {target_user_id} встановлено: {kwh} кВт.")
    except (ValueError, IndexError):
        await message.answer("❌ **Помилка.**\nПеревірте, що `user_id` та сума є числами.")

# --- Обробка геолокації ---
@dp.message(F.location)
async def handle_location(message: types.Message, state: FSMContext):
    await message.answer("🔍 **Шукаємо 3 найближчих об'єкти в Open Charge Map...**")
    stations = await find_three_nearest_stations(message.location.latitude, message.location.longitude)
    
    if stations:
        await state.set_state(BotStates.waiting_for_station_id)
        await message.answer("🎯 **Знайдено 3 найближчих реальних комплекси:**")
        
        for idx, st in enumerate(stations, 1):
            station_text = (
                f"⚡ **Станція #{idx}**\n"
                f"• **Оператор мережі:** ` {st['operator']} `\n"
                f"• **Назва:** {st['name']}\n"
                f"• **Адреса:** {st['address']}\n"
                f"• **Відстань:** **{st['distance']:.2f} км**\n"
                f"• **Роз'єми:** {st['connectors']}\n"
                f"👉 Запуск (надішліть ID): `{st['id']}`"
            )
            await message.answer(station_text, parse_mode="Markdown", reply_markup=get_single_station_keyboard(st['lat'], st['lon']))
            await asyncio.sleep(0.2)
    else:
        await message.answer("❌ Станцій поблизу не знайдено в базі даних Open Charge Map.")

# --- ЕТАП 1 АВТОРІЗАЦІЇ ---
@dp.message(StateFilter(BotStates.waiting_for_station_id))
async def process_station_id(message: types.Message, state: FSMContext):
    station_id = message.text.strip().upper()
    if not station_id.startswith("OCM-"):
        await message.answer("❌ **Невірний формат ID.**\nБудь ласка, введіть ID у форматі `OCM-123456`.", reply_markup=get_charge_menu())
        return

    station_info = await get_station_by_id(station_id)
    
    if station_info:
        name, address, connectors = station_info
        await state.update_data(chosen_station_id=station_id, chosen_station_name=name)
        await state.set_state(BotStates.waiting_for_connector)
        
        await message.answer(
            f"🔌 **Комплекс:** `{name}`\n"
            f"Будь ласка, оберіть роз'єм (кабель), який ви підключили до свого електромобіля:",
            reply_markup=get_connectors_keyboard(connectors),
            parse_mode="Markdown"
        )
    else:
        await message.answer("❌ Станцію з таким ID не знайдено в локальній базі. Спочатку надішліть геопозицію.", reply_markup=get_main_menu())

# --- ЕТАП 2 АВТОРІЗАЦІЇ (Захист від подвійних кліків) ---
@dp.callback_query(lambda c: c.data.startswith('select_conn:'), StateFilter(BotStates.waiting_for_connector))
async def process_connector_selection(callback_query: types.CallbackQuery, state: FSMContext):
    # Захист 2: Рання відповідь на запит Telegram для уникнення зависання кнопки
    await callback_query.answer("Запуск...", cache_time=2)
    await callback_query.message.edit_reply_markup(reply_markup=None)
    
    connector_name = callback_query.data.split(":")[1]
    await state.clear()
    
    cost_kwh = 5.0
    cost_uah = kwh_to_uah(cost_kwh)
    
    balance_uah, _ = await get_user_data(callback_query.from_user.id)
    balance_kwh = uah_to_kwh(balance_uah)

    if balance_kwh < cost_kwh:
        await callback_query.message.answer("❌ Недостатньо кВт·год на рахунку для початку сесії!", reply_markup=get_main_menu())
        return
    
    await callback_query.message.answer(f"⏳ Авторизація сесії... Підключення до порту `{connector_name}`...")
    await asyncio.sleep(1.5)
    
    await update_user_balance(callback_query.from_user.id, -cost_uah, "charge")
    new_balance_kwh = uah_to_kwh(balance_uah - cost_uah)

    await callback_query.message.answer(
        f"✅ **Зарядку успішно активовано!**\n\n"
        f"• **Списано (резерв):** {cost_kwh:.2f} кВт·год\n"
        f"💰 **Залишок:** {new_balance_kwh:.2f} кВт·год",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )

# --- Обробка тарифних пакетів через інвойси ---
@dp.callback_query(lambda c: c.data.startswith('buy_pack_') or c.data == 'activate_night')
async def process_tariff_purchase(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.clear()
    action = callback_query.data
    chat_id = callback_query.message.chat.id

    if action == "buy_pack_50":
        await bot.send_invoice(
            chat_id=chat_id,
            title="🔋 Пакет 50 кВт·год",
            description="Поповнення балансу мережі eVolt UA на 750 грн",
            payload="pack_50",
            provider_token=PAYMENT_TOKEN,
            currency="UAH",
            prices=[types.LabeledPrice(label="Пакет 50 кВт·год", amount=75000)]
        )
    elif action == "buy_pack_100":
        await bot.send_invoice(
            chat_id=chat_id,
            title="🔥 Пакет 100 кВт·год",
            description="Поповнення балансу мережі eVolt UA на 1350 грн (Знижка 10%)",
            payload="pack_100",
            provider_token=PAYMENT_TOKEN,
            currency="UAH",
            prices=[types.LabeledPrice(label="Пакет 100 кВт·год", amount=135000)]
        )
    elif action == "activate_night":
        await set_user_discount(callback_query.from_user.id, 0.85)
        await callback_query.message.answer("🌙 Нічний безліміт підключено")

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    
    kwh_amount = 0.0
    uah_amount = message.successful_payment.total_amount / 100

    if payload == "pack_50": kwh_amount = 50.0
    elif payload == "pack_100": kwh_amount = 100.0

    await update_user_balance(user_id, uah_amount, "deposit")
    await message.answer(f"🎉 **Оплата успішна!**\nНа баланс зараховано **{kwh_amount:.2f} кВт·год**.", reply_markup=get_main_menu())

@dp.message(StateFilter(BotStates.waiting_for_code))
async def process_text_voucher(message: types.Message, state: FSMContext):
    user_code = message.text.strip()
    await state.clear()
    if user_code == "VOLT100":
        await update_user_balance(message.from_user.id, kwh_to_uah(100.0), "deposit")
        balance, _ = await get_user_data(message.from_user.id)
        await message.answer(f"✅ Код прийнято! Баланс: {balance:.2f} грн.", reply_markup=get_main_menu())
    else:
        await message.answer("❌ Невірний код ваучера.", reply_markup=get_main_menu())

# --- Мультимодальна Обробка Голосових Повідомлень ---
@dp.message(F.voice)
async def handle_voice(message: types.Message, state: FSMContext):
    await bot.send_chat_action(chat_id=message.chat.id, action="record_voice")
    ogg_path = f"v_{message.from_user.id}.ogg"
    
    try:
        voice_file = await bot.get_file(message.voice.file_id)
        await bot.download_file(voice_file.file_path, destination=ogg_path)
        
        await message.answer("🎧 *Розпізнаю ваш голос через ШІ...*", parse_mode="Markdown")
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
            
        await message.answer(f"🗣 *Ви сказали:* «{recognized_text}»", parse_mode="Markdown")
        message.text = recognized_text
        
        clean_text = recognized_text.lower()
        if "зарядка" in clean_text:
            await process_charge_click(message)
        elif "ваучер" in clean_text:
            await process_voucher_click(message, state)
        elif "меню" in clean_text or "головне" in clean_text:
            await cmd_back_to_menu(message, state)
        elif "як працює" in clean_text:
            await process_help_click(message)
        elif "підтримка" in clean_text:
            await process_support_click(message)
        else:
            await handle_ai_chat(message)
    except Exception as e:
        logging.error(f"Помилка голосу: {e}")
        await message.answer("🤖 Ой, виникла помилка розпізнавання. Спробуйте написати текстом.")
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

# --- УНІВЕРСАЛЬНИЙ ШІ-ЧАТ ---
@dp.message(lambda m: m.text and not m.text.startswith('/'))
async def handle_ai_chat(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    system_instruction = (
        "Ти — інтелектуальний ШІ-асистент мережі зарядних станцій eVolt UA. "
        "Твоє завдання — максимально корисно відповідати водіям електромобілів. "
        "Ти добре розбираєшся в технічних характеристиках сучасних електромобілів, "
        "включаючи швидкість зарядки та специфіку батарей для таких моделей, як BYD Sealion та Leopard. "
        "Якщо користувач запитує про конкретну локацію (наприклад, чи є зарядка в Зубрі тощо), "
        "використовуй свої знання про інфраструктуру та детально розпиши відомі комплекси чи роз'єми поруч. "
        "Наприкінці відповіді завжди ввічливо додавай, що для пошуку точних станцій мережі в реальному часі "
        "найкраще скористатися кнопкою 'Зарядка ⚡' та надіслати свою геолокацію."
    )
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-3.5-flash',
            contents=message.text,
            config={'system_instruction': system_instruction}
        )
        
        text = response.text
        
        # Захист 3: Обрізання відповідей під ліміт повідомлень Telegram (4096 символів)
        if len(text) > 4000:
            text = text[:4000] + "...\n\n*[Відповідь обрізана через ліміт Telegram]*"
            
        await message.answer(text)
    except Exception as e:
        logging.error(f"Помилка ШІ: {e}")
        await message.answer("🤖 Мій ШІ-модуль перезавантажується. Спробуйте скористатися кнопками меню!")

# --- Старт Проєкту ---
async def main():
    logging.info("Ініціалізація бази даних...")
    await initialize_db()
    
    try:
        print("Бот eVolt UA запущено! Очікування команд...")
        await dp.start_polling(bot)
    finally:
        logging.info("Зупинка бота та очищення ресурсів...")
        await bot.session.close() # Коректне закриття з'єднання з Telegram

if __name__ == "__main__":
    try:
        # Запускаємо основний цикл
        asyncio.run(main())
    except KeyboardInterrupt:
        # Обробка ручної зупинки через Ctrl+C
        print("🛑 Бота зупинено вручну.")
    except asyncio.CancelledError:
        # Ігноруємо технічний "шум" при скасуванні задач
        pass