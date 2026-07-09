import os
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo  # Додай цей імпорт замість timedelta
from aiogram import Router, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from google.genai import types as genai_types

# Імпортуємо ядро з нашого loader
from app.core.loader import bot, ai_client 

from app.keyboards.reply import (
    get_main_menu, get_charge_menu, get_tariffs_keyboard,
    get_single_station_keyboard, get_connectors_keyboard
)
from app.database.connection import ( 
    get_user_data, uah_to_kwh, kwh_to_uah,
    get_station_by_id, update_user_balance, set_user_discount
)
from app.services.ocm_service import find_three_nearest_stations

router = Router()

class BotStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_station_id = State()
    waiting_for_connector = State()

# --- Базові команди меню ---

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    # Отримуємо чистий баланс кВт·год з PostgreSQL
    balance, discount = await get_user_data(user_id)
    
    # 🔥 Створюємо меню прямо тут, щоб назавжди уникнути помилок імпорту
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
    main_menu = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Зарядка ⚡")],
            [KeyboardButton(text="Як працює? 🤨")],
            [KeyboardButton(text="Ваучер 🎫"), KeyboardButton(text="Online підтримка 📢")]
        ],
        resize_keyboard=True
    )
    
    await message.answer(
        f"👋 <b>Доброго дня, {message.from_user.first_name}!</b>\n\n"
        f"🔋 Вітаємо в мережі зарядних станцій eVolt UA.\n"
        f"💰 Ваш доступний запас енергії: <b>{balance} кВт·год</b>\n\n"
        f"Щоб розпочати сесію, введіть ID станції вручну або скористайтеся меню:",
        reply_markup=main_menu,
        parse_mode="HTML"
    )

@router.message(lambda m: m.text and "головне меню" in m.text.lower())
async def cmd_back_to_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Повертаємось до головного меню мережі eVolt UA:", reply_markup=get_main_menu())

@router.message(lambda m: m.text and "як працює" in m.text.lower())
async def process_help_click(message: types.Message):
    await message.answer("ℹ️ **Інструкція мережі eVolt UA:**\n1. Підключіть кабель.\n2. Знайдіть станцію по GPS.\n3. Оберіть роз'єм у чаті для старту сесії.")

@router.message(lambda m: m.text and "підтримка" in m.text.lower())
async def process_support_click(message: types.Message):
    await message.answer("📢 Зв'язок з оператором підтримки eVolt UA: @your_support_username")

# --- Логіка зарядки та пошуку ---

@router.message(lambda m: m.text and "зарядка" in m.text.lower())
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

@router.message(lambda m: m.text and "ввести id" in m.text.lower())
async def manual_id_entry(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_station_id)
    await message.answer("Введіть ID зарядної станції (наприклад: `OCM-307584`):")

@router.message(F.location)
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

@router.message(StateFilter(BotStates.waiting_for_station_id))
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

@router.callback_query(lambda c: c.data.startswith('select_conn:'), StateFilter(BotStates.waiting_for_connector))
async def process_connector_selection(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer("Запуск...", cache_time=2)
    await callback_query.message.edit_reply_markup(reply_markup=None)
    
    connector_name = callback_query.data.split(":", 1)[1]
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

# --- Тарифи, Ваучери та Платежі ---

@router.message(lambda m: m.text and "ваучер" in m.text.lower())
async def process_voucher_click(message: types.Message, state: FSMContext):
    balance_uah, _ = await get_user_data(message.from_user.id)
    balance_kwh = uah_to_kwh(balance_uah)
    
    await message.answer(
        f"💳 **Ваш balance:** `{balance_kwh:.2f} кВт·год`\n\n"
        f"🎁 Оберіть тарифний пакет:",
        reply_markup=get_tariffs_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(BotStates.waiting_for_code)

@router.callback_query(lambda c: c.data.startswith('buy_pack_') or c.data == 'activate_night')
async def process_tariff_purchase(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.clear()
    action = callback_query.data
    chat_id = callback_query.message.chat.id
    payment_token = os.getenv("PAYMENT_PROVIDER_TOKEN")

    if action == "buy_pack_50":
        await bot.send_invoice(
            chat_id=chat_id,
            title="🔋 Пакет 50 кВт·год",
            description="Поповнення балансу мережі eVolt UA на 750 грн",
            payload="pack_50",
            provider_token=payment_token,
            currency="UAH",
            prices=[types.LabeledPrice(label="Пакет 50 кВт·год", amount=75000)]
        )
    elif action == "buy_pack_100":
        await bot.send_invoice(
            chat_id=chat_id,
            title="🔥 Пакет 100 кВт·год",
            description="Поповнення балансу мережі eVolt UA на 1350 грн (Знижка 10%)",
            payload="pack_100",
            provider_token=payment_token,
            currency="UAH",
            prices=[types.LabeledPrice(label="Пакет 100 кВт·год", amount=135000)]
        )
    elif action == "activate_night":
        await set_user_discount(callback_query.from_user.id, 0.85)
        await callback_query.message.answer("🌙 Нічний безліміт підключено")

@router.message(StateFilter(BotStates.waiting_for_code))
async def process_text_voucher(message: types.Message, state: FSMContext):
    user_code = message.text.strip()
    await state.clear()
    if user_code == "VOLTie100" or user_code == "VOLT100":
        await update_user_balance(message.from_user.id, kwh_to_uah(100.0), "deposit")
        balance, _ = await get_user_data(message.from_user.id)
        await message.answer(f"✅ Код прийнято! Баланс оновлено.", reply_markup=get_main_menu())
    else:
        await message.answer("❌ Невірний код ваучера.", reply_markup=get_main_menu())

# --- Обробка платіжних інвойсів Telegram ---

@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    
    # Скільки грошей прийшло від Telegram (750 або 1350)
    uah_amount = message.successful_payment.total_amount / 100
    # Визначаємо чистий об'єм кВт·год для тексту повідомлення
    kwh_amount = 50.0 if payload == "pack_50" else 100.0
    
    # Нараховуємо суму в базу даних (750 одиниць бази = 50 кВт·год)
    await update_user_balance(user_id, uah_amount, f"buy_{payload}")
    
    await message.answer(
        f"🎉 <b>Пакет активовано успішно!</b>\n\n"
        f"🔋 На Ваш рахунок зараховано: <b>{kwh_amount} кВт·год</b>.\n"
        f"⚡ Кнопка «Зарядка» активована. Приємної подорожі!",
        parse_mode="HTML"
    )
# # --- Команда історії операцій ---

@router.message(Command("history"))
async def cmd_history(message: types.Message):
    user_id = message.from_user.id
    
    # 💥 Замість неіснуючого "db" використовуємо наш пул підключень
    from app.database.connection import pool
    
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT amount, transaction_type, created_at FROM transactions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 5",
            user_id
        )
        
    if not rows:
        await message.answer("📜 <b>Історія операцій порожня.</b>", parse_mode="HTML")
        return
        
    text = "📜 <b>Останні 5 операцій:</b>\n\n"
    for row in rows:
        # Гарно форматуємо знак плюс для поповнень
        sign = "+" if row['amount'] > 0 else ""
        date_str = row['created_at'].strftime("%d.%m.%Y %H:%M")
        
        text += f"📅 {date_str} | <b>{sign}{row['amount']:.2f} грн</b> ({row['transaction_type']})\n"
        
    await message.answer(text, parse_mode="HTML")
# --- Голосове керування через Gemini ---

@router.message(F.voice)
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
        await message.answer("🤖 Виникла помилка розпізнавання. Спробуйте написати текстом.")
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

# --- Універсальний ШІ-чат ---

@router.message(lambda m: m.text and not m.text.startswith('/'))
async def handle_ai_chat(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    system_instruction = (
        "Ти — інтелектуальний ШІ-асистент мережі зарядних станцій eVolt UA. "
        "Твоє завдання — максимально корисно відповідати водіям електромобілів. "
        "Ти добре розбираєшся в технічних характеристиках сучасних електромобілів, "
        "включаючи швидкість зарядки та специфіку батарей. "
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
        if len(text) > 4000:
            text = text[:4000] + "...\n\n*[Відповідь обрізана через ліміт Telegram]*"
        await message.answer(text)
    except Exception as e:
        logging.error(f"Помилка ШІ: {e}")
        await message.answer("🤖 Мій ШІ-модуль перезавантажується. Спробуйте скористатися кнопками меню!")