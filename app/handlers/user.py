import os
import json
import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from aiogram import Router, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from google.genai import types as genai_types

from app.core.loader import bot, ai_client 

from app.keyboards.reply import (
    get_main_menu, get_charge_menu, get_tariffs_keyboard,
    get_single_station_keyboard, get_connectors_keyboard
)

import app.database.connection as db_conn
from app.database.connection import (
    get_user_data, uah_to_kwh, kwh_to_uah,
    get_station_by_id, set_user_discount
)
from app.database import operators_repo as op_repo
from app.services.ocm_service import find_three_nearest_stations
from app.services.station_speed import classify_station_speed

# Публічний URL сервісу для посилання на оплату QR (та сама логіка, що й
# app/api/driver_qr.py та app/handlers/operator_billing.py).
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL") or os.getenv("EMSP_BASE_URL") or "https://evolt.ua"
).rstrip("/")

# Радіус пошуку станцій White-Label операторів навколо водія (Промпт 4c).
# OCM обмежений через свій API-параметр distance, тут — свій радіус.
OPERATOR_SEARCH_RADIUS_KM = 30

router = Router()

class BotStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_station_id = State()
    waiting_for_connector = State()

# --- Базові команди меню (з підтримкою скидання будь-яких станів) ---

@router.message(Command("start"), StateFilter("*"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    balance, discount = await get_user_data(user_id)

    # Раніше тут будувалось власне, окреме ReplyKeyboardMarkup з іншими
    # емодзі, ніж у get_main_menu() (наприклад, "Ваучер 🎫" тут проти
    # "Ваучер 🧾" в get_main_menu()) — два незалежних джерела правди для
    # того самого меню, які легко розсинхронити (нову кнопку "Баланс"
    # довелось би додавати в двох місцях). Тепер обидва місця використовують
    # єдиний get_main_menu().
    await message.answer(
        f"👋 <b>Доброго дня, {message.from_user.first_name}!</b>\n\n"
        f"🔋 Вітаємо в мережі зарядних станцій eVolt UA.\n"
        f"💰 Загальний баланс: <b>{balance:.2f} кВт·год</b>\n\n"
        f"Щоб розпочати сесію, введіть ID станції вручну або скористайтеся меню:",
        reply_markup=get_main_menu(),
        parse_mode="HTML"
    )

@router.message(lambda m: m.text and "головне меню" in m.text.lower(), StateFilter("*"))
async def cmd_back_to_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Повертаємось до головного меню мережі eVolt UA:", reply_markup=get_main_menu())

@router.message(lambda m: m.text and "як працює" in m.text.lower(), StateFilter("*"))
async def process_help_click(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("ℹ️ **Інструкція мережі eVolt UA:**\n1. Підключіть кабель.\n2. Знайдіть станцію по GPS.\n3. Оберіть роз'єм у чаті для старту сесії.")

@router.message(lambda m: m.text and "підтримка" in m.text.lower(), StateFilter("*"))
async def process_support_click(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("📢 Зв'язок з оператором підтримки eVolt UA: @your_support_username")

# --- Команди зі списку "Menu" в Telegram (bot.set_my_commands у app/main.py) ---
# Дублюють натискання відповідних кнопок reply-клавіатури (app/keyboards/reply.py),
# щоб вибір команди зі списку "Menu" біля поля вводу реально працював, а не
# лише показував пункт у списку.

@router.message(Command("balance"), StateFilter("*"))
async def cmd_balance_menu(message: types.Message, state: FSMContext):
    await process_balance_click(message, state)

@router.message(Command("charge"), StateFilter("*"))
async def cmd_charge_menu(message: types.Message, state: FSMContext):
    await process_charge_click(message, state)

@router.message(Command("voucher"), StateFilter("*"))
async def cmd_voucher_menu(message: types.Message, state: FSMContext):
    await process_voucher_click(message, state)

@router.message(Command("support"), StateFilter("*"))
async def cmd_support_menu(message: types.Message, state: FSMContext):
    await process_support_click(message, state)

# --- Логіка зарядки та списання ---

@router.message(lambda m: m.text and "зарядка" in m.text.lower(), StateFilter("*"))
async def process_charge_click(message: types.Message, state: FSMContext):
    await state.clear()  # Витягуємо користувача з будь-якого завислого стану
    balance, _ = await get_user_data(message.from_user.id)
    if balance <= 0:
        await message.answer(f"❌ **Недостатньо коштів.**\nБаланс: {balance:.2f} кВт·год.\nБудь ласка, поповніть рахунок у меню Ваучер 🎫.")
    else:
        await message.answer(
            "🔌 **Оберіть спосіб пошуку станції:**\n\n"
            "• Надішліть геопозицію, і бот знайде станції.\n"
            "• Або введіть ID вручну.",
            reply_markup=get_charge_menu()
        )

@router.message(lambda m: m.text and "ввести id" in m.text.lower(), StateFilter("*"))
async def manual_id_entry(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.waiting_for_station_id)
    await message.answer("Введіть ID зарядної станції (наприклад: `OCM-307584`):")

def _merge_search_results(ocm_stations, operator_stations):
    """
    Обʼєднує видачу OCM і White-Label операторських станцій (Промпт 4c) в
    один список, відсортований за відстанню. Чиста функція — без Telegram і
    без БД, щоб «змішана видача OCM+оператор» тестувалась без моків бота.

    Кожен елемент: {"source": "ocm"|"operator", "distance_km": float, "station": <dict>}.
    OCM без відстані (малоймовірно, але API цього формально не гарантує)
    відсувається в кінець, а не ламає сортування.
    """
    items = []
    for st in ocm_stations or []:
        distance = st.get("distance")
        items.append({
            "source": "ocm",
            "distance_km": float(distance) if distance is not None else float("inf"),
            "station": st,
        })
    for st in operator_stations or []:
        items.append({"source": "operator", "distance_km": float(st["distance_km"]), "station": st})
    items.sort(key=lambda item: item["distance_km"])
    return items


def _format_operator_station_card(idx: int, station: dict) -> str:
    """
    Картка станції оператора: бейдж, відстань, потужність/конектор, тариф і
    QR-посилання на оплату.

    Назва станції й тип конектора — вільний текст, який оператор увів сам у
    майстрі станції (app/handlers/operator_billing.py). Без html.escape()
    символ '<' у назві ламає парсинг HTML і Telegram взагалі не надсилає
    повідомлення (send message: can't parse entities), а водій замість
    картки не бачить нічого. Гірше — сюди можна вставити довільний тег
    (напр. <a href=...>) і показати його водіям, які нічого не підозрюють.
    """
    badge = classify_station_speed(station.get("power_kw"), station.get("connector_type"))
    prefix = f"{badge} " if badge else ""
    name = html.escape(station["name"])
    lines = [
        f"{prefix}<b>Станція #{idx}: {name}</b>",
        f"📍 Відстань: <b>{station['distance_km']:.2f} км</b>",
    ]
    if station.get("power_kw"):
        lines.append(f"⚙️ Потужність: {station['power_kw']} кВт")
    if station.get("connector_type"):
        lines.append(f"🔌 Конектор: {html.escape(station['connector_type'])}")
    lines.append(f"💰 Тариф: {station['tariff_uah_kwh']} грн/кВт·год")
    lines.append("💳 Оплата через QR:")
    lines.append(f"{PUBLIC_BASE_URL}/s/{station['qr_slug']}")
    return "\n".join(lines)


def _format_ocm_station_card(idx: int, station: dict) -> str:
    """Картка станції OCM — той самий формат, що й раніше, плюс бейдж швидкості спереду."""
    badge = classify_station_speed(station.get("power_kw"), station.get("connector_type"))
    prefix = f"{badge} " if badge else ""
    return (
        f"{prefix}⚡ **Станція #{idx}**\n"
        f"• **Оператор мережі:** ` {station['operator']} `\n"
        f"• **Назва:** {station['name']}\n"
        f"• **Адреса:** {station['address']}\n"
        f"• **Відстань:** **{station['distance']:.2f} км**\n"
        f"• **Роз'єми:** {station['connectors']}\n"
        f"👉 Запуск (надішліть ID): `{station['id']}`"
    )


@router.message(F.location, StateFilter("*"))
async def handle_location(message: types.Message, state: FSMContext):
    await message.answer("🔍 **Шукаємо станції поруч...**")
    lat, lon = message.location.latitude, message.location.longitude

    ocm_stations = await find_three_nearest_stations(lat, lon) or []
    operator_stations = await op_repo.list_public_stations_near(lat, lon, OPERATOR_SEARCH_RADIUS_KM)
    combined = _merge_search_results(ocm_stations, operator_stations)

    if not combined:
        await message.answer("❌ Станцій поблизу не знайдено.")
        return

    # Ручний запуск за ID (наступний крок FSM) працює лише для OCM-станцій —
    # операторську станцію водій оплачує через QR/посилання, а не введенням ID.
    if any(item["source"] == "ocm" for item in combined):
        await state.set_state(BotStates.waiting_for_station_id)

    await message.answer(f"🎯 **Знайдено {len(combined)} станцій поруч:**")

    for idx, item in enumerate(combined, 1):
        station = item["station"]
        if item["source"] == "operator":
            await message.answer(_format_operator_station_card(idx, station), parse_mode="HTML")
        else:
            await message.answer(
                _format_ocm_station_card(idx, station), parse_mode="Markdown",
                reply_markup=get_single_station_keyboard(station["lat"], station["lon"]),
            )
        await asyncio.sleep(0.2)

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

# --- Хендлер вибору роз'єму ---

@router.callback_query(lambda c: c.data.startswith('select_conn:'), StateFilter(BotStates.waiting_for_connector))
async def process_connector_selection(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer("Обробка...", cache_time=2)
    await callback_query.message.edit_reply_markup(reply_markup=None)
    
    connector_name = callback_query.data.split(":", 1)[1]
    
    state_data = await state.get_data()
    station_id = state_data.get("chosen_station_id", "LOC-001")
    station_name = state_data.get("chosen_station_name", "⚡ Ево-Заряд Комплекс")
    
    await state.clear()
    
    cost_kwh = 5.0
    user_id = callback_query.from_user.id
    
    balance_kwh, _ = await get_user_data(user_id)

    if balance_kwh < cost_kwh:
        await callback_query.message.answer("❌ Недостатньо кВт·год на рахунку для початку сесії!", reply_markup=get_main_menu())
        return
    
    text = (
        f"🏢 <b>Зарядна станція:</b> {station_name}\n"
        f"🔌 <b>Обраний роз'єм:</b> <code>{connector_name}</code>\n"
        f"💳 <b>Вартість старту:</b> {cost_kwh:.2f} кВт·год\n"
        f"🟢 <b>Статус:</b> Готова до запуску\n\n"
        f"Переконайся, що кабель підключено до авто, та натисни кнопку нижче:"
    )
    
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⚡ Запустити зарядку", 
                callback_data=f"ocpi_start_{station_id}:{connector_name}:{cost_kwh}"
            )
        ],
        [
            InlineKeyboardButton(text="🔄 Скасувати", callback_data=f"ocpi_refresh_{station_id}")
        ]
    ])

    await callback_query.message.answer(text, parse_mode="HTML", reply_markup=confirm_keyboard)

# --- Тарифи, Ваучери та Платежі ---

@router.message(lambda m: m.text and "ваучер" in m.text.lower(), StateFilter("*"))
async def process_voucher_click(message: types.Message, state: FSMContext):
    await state.clear()
    balance_kwh, _ = await get_user_data(message.from_user.id)
    
    await message.answer(
        f"💳 **Ваш загальний баланс:** `{balance_kwh:.2f} кВт·год`\n\n"
        f"🎁 Оберіть тарифний пакет:",
        reply_markup=get_tariffs_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(BotStates.waiting_for_code)

@router.callback_query(lambda c: c.data.startswith('buy_pack_') or c.data == 'activate_night', StateFilter("*"))
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
    await state.clear()
    user_code = (message.text or "").strip()
    lowered = user_code.lower()

    # Раніше цей хендлер (фільтр — лише StateFilter, без перевірки тексту)
    # ковтав БУДЬ-яке повідомлення, поки бот "чекав код ваучера" — включно
    # з натисканням інших кнопок головного меню (Баланс, Зарядка тощо),
    # показуючи їм помилково "Невірний код ваучера" замість переходу в
    # потрібний розділ. Тепер спершу перевіряємо, чи це не інша кнопка меню.
    if "баланс" in lowered:
        await process_balance_click(message, state)
        return
    if "зарядка" in lowered:
        await process_charge_click(message, state)
        return
    if "ваучер" in lowered:
        await process_voucher_click(message, state)
        return
    if "підтримка" in lowered:
        await process_support_click(message, state)
        return
    if "головне" in lowered or "меню" in lowered:
        await cmd_back_to_menu(message, state)
        return

    user_id = message.from_user.id

    if user_code in ["VOLTie100", "VOLT100"]:
        bonus_kwh = 100.0
        async with db_conn.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", bonus_kwh, user_id)
                await conn.execute("""
                    INSERT INTO kw_transactions (user_id, type, amount, description) 
                    VALUES ($1, 'deposit', $2, $3)
                """, user_id, bonus_kwh, f"Активація текстового ваучера {user_code}")
                
        await message.answer(f"✅ Код прийнято! Нараховано +100.00 кВт·год.", reply_markup=get_main_menu())
    else:
        await message.answer("❌ Невірний код ваучера.", reply_markup=get_main_menu())

# --- Обробка платіжних інвойсів Telegram ---

@router.pre_checkout_query(StateFilter("*"))
async def process_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment, StateFilter("*"))
async def process_successful_payment(message: types.Message):
    """
    Раніше цей хендлер писав напряму: UPDATE users SET balance = ... +
    INSERT INTO kw_transactions, в обхід і update_user_balance(), і таблиці
    payments — тобто жодного запису про сам платіж Telegram (суму, валюту,
    унікальний ID від Telegram) взагалі не зберігалось. Це третій, ніким не
    помічений шлях запису балансу в обхід єдиної точки (після OCPI-CDR і
    Monobank-webhook, які вже виправлялись раніше) — і оскільки платежу немає
    в `payments`, реконсиляція (reconcile_payments.py) не могла б його
    перевірити взагалі. Тепер: спершу фіксуємо сам платіж у payments
    (invoice_id = унікальний telegram_payment_charge_id — Telegram гарантує
    його унікальність і незмінність), потім нараховуємо кВт·год через
    update_user_balance() з прив'язкою payment_id, як і для Monobank.
    """
    user_id = message.from_user.id
    sp = message.successful_payment
    payload = sp.invoice_payload
    kwh_amount = 50.0 if payload == "pack_50" else 100.0
    amount_uah = sp.total_amount / 100  # Telegram теж присилає суму в копійках

    async with db_conn.db_pool.acquire() as conn:
        async with conn.transaction():
            existing_payment = await conn.fetchrow(
                "SELECT id FROM payments WHERE invoice_id = $1",
                sp.telegram_payment_charge_id,
            )
            if existing_payment:
                logging.info(
                    f"Telegram-платіж {sp.telegram_payment_charge_id} вже оброблений раніше. Пропускаємо."
                )
                return

            payment_id = await conn.fetchval(
                """
                INSERT INTO payments (user_id, invoice_id, amount, provider, status, payload, created_at, updated_at)
                VALUES ($1, $2, $3, 'telegram', 'success', $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
                """,
                user_id, sp.telegram_payment_charge_id, amount_uah,
                json.dumps({
                    "invoice_payload": payload,
                    "provider_payment_charge_id": sp.provider_payment_charge_id,
                    "currency": sp.currency,
                }),
            )
            await db_conn.update_user_balance(
                user_id=user_id,
                amount_kwh=kwh_amount,
                t_type="deposit",
                conn=conn,
                payment_id=payment_id,
                description=f"Поповнення через Telegram Invoice ({payload})",
            )

    await message.answer(
        f"🎉 <b>Пакет активовано успішно!</b>\n\n"
        f"🔋 На Ваш рахунок зараховано: <b>{kwh_amount} кВт·год</b>.\n"
        f"⚡ Поточний баланс оновлено.",
        parse_mode="HTML"
    )

# --- Баланс та історія операцій ---

async def _build_balance_and_history_text(user_id: int) -> str:
    """
    Спільна логіка для кнопки "Баланс" та команди /history, щоб не
    дублювати формат балансу втретє (після /start і кнопки "Ваучер").

    Раніше `sign`/`op_type` вважали "не-deposit" операцію завжди
    "Зарядка/Витрата" з мінусом — це коректно для withdrawal/ocpi_session,
    але некоректно для типу 'refund' (доданого пізніше цієї сесії): рефанд
    це нарахування користувачу, а показувався б як "-" списання. Виправлено.
    """
    balance_kwh, _ = await get_user_data(user_id)

    async with db_conn.db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT amount, type, created_at
            FROM kw_transactions
            WHERE user_id = $1
            ORDER BY created_at DESC LIMIT 5
        """, user_id)

    if not rows:
        return f"💳 <b>Ваш поточний баланс:</b> <code>{balance_kwh:.2f} кВт·год</code>\n\n📜 <b>Історія операцій порожня.</b>"

    text = f"💳 <b>Ваш поточний баланс:</b> <code>{balance_kwh:.2f} кВт·год</code>\n\n📜 <b>Останні 5 Ledger-операцій (кВт·год):</b>\n\n"
    for row in rows:
        is_credit = row['type'] in ("deposit", "refund")
        sign = "+" if is_credit else "-"
        date_str = row['created_at'].strftime("%d.%m.%Y %H:%M")
        if row['type'] == 'deposit':
            op_type = "Поповнення"
        elif row['type'] == 'refund':
            op_type = "Повернення коштів"
        else:
            op_type = "Зарядка/Витрата"

        text += f"📅 {date_str} | <b>{sign}{abs(row['amount']):.2f} кВт·год</b> ({op_type})\n"

    return text


@router.message(lambda m: m.text and "баланс" in m.text.lower(), StateFilter("*"))
async def process_balance_click(message: types.Message, state: FSMContext):
    await state.clear()
    text = await _build_balance_and_history_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu())


@router.message(Command("history"), StateFilter("*"))
async def cmd_history(message: types.Message):
    text = await _build_balance_and_history_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML")

# --- Голосове керування через Gemini ---

@router.message(F.voice, StateFilter("*"))
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
            await process_charge_click(message, state)
        elif "баланс" in clean_text:
            await process_balance_click(message, state)
        elif "ваучер" in clean_text:
            await process_voucher_click(message, state)
        elif "меню" in clean_text or "головне" in clean_text:
            await cmd_back_to_menu(message, state)
        elif "як працює" in clean_text:
            await process_help_click(message, state)
        elif "підтримка" in clean_text:
            await process_support_click(message, state)
        else:
            await handle_ai_chat(message)
    except Exception as e:
        logging.error(f"Помилка голосу: {e}")
        await message.answer("🤖 Виникла помилка розпізнавання. Спробуйте написати текстом.")
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

# --- Універсальний ШІ-чат ---

@router.message(lambda m: m.text and not m.text.startswith('/'), StateFilter("*"))
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
