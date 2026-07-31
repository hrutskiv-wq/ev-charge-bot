import os
import json
import asyncio
import html
import logging
from decimal import Decimal
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
    get_single_station_keyboard, get_search_results_keyboard, get_connectors_keyboard
)

import app.database.connection as db_conn
from app.database.connection import (
    get_user_data, uah_to_kwh, kwh_to_uah,
    get_station_by_id, set_user_discount
)
from app.database import operators_repo as op_repo
from app.core.crypto import EncryptionKeyMissing, decrypt_secret
from app.services.monobank_acquiring import MonobankError, create_invoice
from app.services.ocm_service import find_three_nearest_stations, ATTRIBUTION_TEXT as OCM_ATTRIBUTION_TEXT
from app.services.station_speed import classify_station_speed
from app.services.geo import haversine_km
from app.services import tomtom_service

# Публічний URL сервісу для посилання на оплату QR (та сама логіка, що й
# app/api/driver_qr.py та app/handlers/operator_billing.py).
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL") or os.getenv("EMSP_BASE_URL") or "https://evolt.ua"
).rstrip("/")

# Радіус пошуку станцій White-Label операторів навколо водія (Промпт 4c).
# OCM обмежений через свій API-параметр distance, тут — свій радіус.
OPERATOR_SEARCH_RADIUS_KM = 30

# TomTom — живий інфошар (не БД, не кеш — app/services/tomtom_service.py).
# TOMTOM_SEARCH_LIMIT — розмір ОДНІЄЇ відповіді nearbySearch, не кількість
# HTTP-викликів (той самий один запит незалежно від значення) — піднятий
# до 10, щоб після дедупу з OCM (_dedupe_by_proximity нижче) лишалось із
# чого вибирати квоту (_select_by_quota). Реальний бюджет квоти TomTom
# обмежує МАКСИМАЛЬНА кількість станцій, що йдуть у видачу
# (MAX_STATIONS_TOTAL) — саме стільки додаткових викликів get_availability()
# робиться щонайбільше.
TOMTOM_SEARCH_RADIUS_KM = 15
TOMTOM_SEARCH_LIMIT = 10

# OCM жорстко обмежував пул трьома станціями (DEFAULT_MAXRESULTS у
# ocm_service.py) — з трьох не набрати 4 DC у квоту нижче. Ширший пул
# кандидатів, з якого вже відбирає _select_by_quota; сам параметр
# продубльований у ключі кешу OCM (ocm_service.py::location_key_builder),
# інакше різні maxresults для тих самих координат ділили б один кеш.
OCM_SEARCH_MAXRESULTS = 10

# Дві станції з РІЗНИХ джерел (OCM/TomTom/оператор) ближче цієї відстані
# одна від одної вважаються однією фізичною зарядною станцією, що просто
# потрапила в нашу видачу з кількох каталогів одразу.
DEDUPE_DISTANCE_KM = 0.1

# --- Квота видачі (живий смоук 31.07.2026) ---
#
# Сортування суто за відстанню (старий підхід — простий зріз [:N]) витісняло
# швидкі DC-станції сусідніми повільними AC. _select_by_quota нижче відбирає
# замість зрізу: оператор -> DC -> AC, кожен блок за відстанню в межах своєї
# квоти. Діє в ОБОХ режимах рендеру (SEARCH_SINGLE_MESSAGE нижче) — це відбір
# станцій, а не спосіб їх показу.
MAX_OPERATOR_SHOWN = 2
MAX_DC_SHOWN = 4
MAX_STATIONS_TOTAL = 6

# Поріг для _is_dc_station — той самий поріг, що FAST_POWER_THRESHOLD_KW у
# app/services/station_speed.py, число продубльоване навмисно:
# classify_station_speed вирішує ЩО ПОКАЗАТИ (бейдж картки), а
# _is_dc_station — ЩО ВІДІБРАТИ в квоту; тримати їх незалежними один від
# одного безпечніше, ніж імпортувати внутрішню константу чужого модуля,
# бо зміна бейджа не повинна мовчки міняти відбір квоти, і навпаки.
DC_POWER_THRESHOLD_KW = 50
# Ті самі хінти, що FAST_CONNECTOR_HINTS у station_speed.py — свідомо саме
# "GB/T DC" (той запис, що вже є в кодовій базі), щоб один і той самий
# фізичний тип конектора не отримав дві різні текстові форми.
DC_CONNECTOR_HINTS = ("CCS", "CHAdeMO", "GB/T DC")

# ПРАВКА 7: прапорець рендеру видачі — той самий прецедент, що
# TELEGRAM_PAYMENTS_ENABLED нижче. Будь-яке значення, крім "false" (без
# урахування регістру, включно з незаданим env), — новий рендер (одне
# HTML-повідомлення). "false" — старий рендер (окрема картка на станцію,
# як зараз на проді) без деплою нового коду, лише зміна env — відкат-запобіжник.
SEARCH_SINGLE_MESSAGE = os.getenv("SEARCH_SINGLE_MESSAGE", "true").strip().lower() != "false"

# --- Купівля kWh-пакетів (buy-side гаманця) через Monobank-еквайринг ---
#
# Оператор, чиїм мерчант-токеном виставляється інвойс за пакет — той самий
# механізм, що й у станційному QR-флоу (app/api/driver_qr.py), лише гроші
# йдуть не на станцію, а прямо на поповнення kWh-гаманця водія. id береться
# з env, а не хардкодиться: конкретний рядок operators, що є "оператором №0"
# (пілот eVolt на собі), відомий лише на розгорнутій базі.
WALLET_OPERATOR_ID = int(os.getenv("WALLET_OPERATOR_ID") or 0)

# Тестовий Telegram Payments флоу (send_invoice/successful_payment) лишається
# в коді, але вимкнений за замовчуванням — тепер купівля пакетів іде через
# живий Monobank-еквайринг. Вмикається лише явно, якщо колись знадобиться
# повернутись до нього.
TELEGRAM_PAYMENTS_ENABLED = os.getenv("TELEGRAM_PAYMENTS_ENABLED", "0") == "1"

# Ключі словника — це callback_data кнопок (app/keyboards/reply.py::
# get_tariffs_keyboard). "code" — канонічне ім'я пакета для БД/банку
# (wallet_topups.package має CHECK IN ('pack_50', 'pack_100') — навмисно
# НЕ прив'язане до конкретного рядка callback_data, щоб зміна тексту чи
# callback_data кнопки в майбутньому не вимагала міграції схеми).
WALLET_PACKAGES = {
    "buy_pack_50": {"code": "pack_50", "kwh": 50.0, "amount_uah": Decimal("750.00"),
                    "title": "Пакет 50 кВт·год"},
    "buy_pack_100": {"code": "pack_100", "kwh": 100.0, "amount_uah": Decimal("1350.00"),
                     "title": "Пакет 100 кВт·год (знижка 10%)"},
}

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

def _merge_search_results(ocm_stations, operator_stations, tomtom_stations=None):
    """
    Обʼєднує видачу OCM, TomTom (живий інфошар) і White-Label операторських
    станцій (Промпт 4c) в один список, відсортований за відстанню. Чиста
    функція — без Telegram і без БД, щоб «змішана видача» тестувалась без
    моків бота.

    Кожен елемент: {"source": "ocm"|"operator"|"tomtom", "distance_km": float, "station": <dict>}.
    Станції без відстані відсуваються в кінець, а не ламають сортування.
    Список для сортування будується в порядку пріоритету джерел (оператор —
    шар дії, тоді TomTom, тоді OCM); сортування стабільне, тож при РІВНІЙ
    відстані цей порядок зберігається — операторські станції лишаються
    першими серед рівновіддалених.
    """
    items = []
    for st in operator_stations or []:
        items.append({"source": "operator", "distance_km": float(st["distance_km"]), "station": st})
    for st in tomtom_stations or []:
        distance = st.get("distance_km")
        items.append({
            "source": "tomtom",
            "distance_km": float(distance) if distance is not None else float("inf"),
            "station": st,
        })
    for st in ocm_stations or []:
        distance = st.get("distance")
        items.append({
            "source": "ocm",
            "distance_km": float(distance) if distance is not None else float("inf"),
            "station": st,
        })
    items.sort(key=lambda item: item["distance_km"])
    return items


def _station_coords(station: dict):
    """(lat, lon) станції незалежно від джерела. Операторські станції
    (app/database/operators_repo.py::list_public_stations_near) несуть
    довготу під ключем `lng` (як колонка в БД), OCM і TomTom — під `lon`;
    без цієї різниці дедуп ніколи не бачив би координат операторської
    станції й правило «оператор перемагає завжди» не спрацьовувало б саме
    для дублів із операторським джерелом."""
    lat = station.get("lat")
    lon = station.get("lon")
    if lon is None:
        lon = station.get("lng")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def _metadata_score(station: dict) -> int:
    """Скільки корисних технічних метаданих має станція — критерій вибору
    переможця дедупу між OCM і TomTom (операторська перемагає обидві
    завжди, незалежно від цього)."""
    score = 0
    if station.get("power_kw"):
        score += 1
    if station.get("connector_type"):
        score += 1
    return score


def _dedupe_winner(a: dict, b: dict) -> dict:
    """Хто з двох дублікатів (елементів _merge_search_results, РІЗНІ
    джерела) лишається у видачі: (1) операторська станція — завжди, це шар
    дії з оплатою через бот; (2) інакше — та, у якої БІЛЬШЕ метаданих
    (потужність/тип конектора); (3) при рівності — TomTom, бо може нести
    live-статус доступності конектора, якого в OCM немає взагалі."""
    if a["source"] == "operator":
        return a
    if b["source"] == "operator":
        return b
    score_a = _metadata_score(a["station"])
    score_b = _metadata_score(b["station"])
    if score_a != score_b:
        return a if score_a > score_b else b
    return a if a["source"] == "tomtom" else b


def _dedupe_by_proximity(items):
    """
    Прибирає дублікати ОДНІЄЇ фізичної станції, що прийшла з КІЛЬКОХ джерел
    одразу (типово: та сама заправка є і в OCM, і в TomTom). Чиста функція
    — без Telegram і без БД.

    `items` — вихід `_merge_search_results` (уже відсортований за
    `distance_km`). Дві станції РІЗНИХ джерел ближче `DEDUPE_DISTANCE_KM`
    одна від одної вважаються однією фізичною станцією — лишається одна,
    за правилом `_dedupe_winner`. Станції ОДНОГО джерела між собою не
    звіряються (не мета цієї функції — власні дублі кожне джерело або не
    дає взагалі, або це вже його власний баг).
    """
    kept = []
    for item in items:
        coords = _station_coords(item["station"])
        duplicate_idx = None
        if coords is not None:
            for idx, existing in enumerate(kept):
                if existing["source"] == item["source"]:
                    continue
                existing_coords = _station_coords(existing["station"])
                if existing_coords is None:
                    continue
                if haversine_km(*coords, *existing_coords) < DEDUPE_DISTANCE_KM:
                    duplicate_idx = idx
                    break

        if duplicate_idx is None:
            kept.append(item)
        elif _dedupe_winner(kept[duplicate_idx], item) is item:
            kept[duplicate_idx] = item

    return kept


def _is_dc_station(station: dict) -> bool:
    """
    Предикат ДЛЯ ВІДБОРУ квоти (_select_by_quota) — окремий від
    classify_station_speed (app/services/station_speed.py), яка вирішує ЩО
    ПОКАЗАТИ на картці (бейдж), а не що відібрати в квоту; та функція тут
    НЕ чіпається.

    DC, якщо виконано БУДЬ-ЩО з:
    - у якогось конектора станції TomTom-специфічний `current_type == "DC"`
      (station["connectors"] — список словників лише в нормалізації TomTom,
      app/services/tomtom_service.py; для OCM/оператора це поле або рядок,
      або взагалі відсутнє — isinstance-перевірка нижче тому обов'язкова,
      інакше рядок OCM ітерувався б по символах);
    - `power_kw` станції >= DC_POWER_THRESHOLD_KW;
    - `connector_type` станції містить один із DC_CONNECTOR_HINTS.

    Станція без жодних із цих даних — AC за замовчуванням (не вгадуємо).
    """
    connectors = station.get("connectors")
    if isinstance(connectors, list):
        for connector in connectors:
            if isinstance(connector, dict) and connector.get("current_type") == "DC":
                return True

    power = station.get("power_kw")
    if power is not None:
        try:
            if float(power) >= DC_POWER_THRESHOLD_KW:
                return True
        except (TypeError, ValueError):
            pass

    connector_type = station.get("connector_type")
    if connector_type:
        lowered = connector_type.lower()
        if any(hint.lower() in lowered for hint in DC_CONNECTOR_HINTS):
            return True

    return False


def _select_by_quota(items):
    """
    Відбір у видачу за квотою (живий смоук 31.07.2026) ЗАМІСТЬ зрізу [:N] за
    відстанню — простий зріз витісняв швидкі DC-станції сусідніми
    повільними AC. `items` — вихід `_dedupe_by_proximity` (уже
    відсортований за `distance_km`), результат зберігає це впорядкування
    В МЕЖАХ КОЖНОГО БЛОКУ.

    1) операторські станції — завжди першими, максимум MAX_OPERATOR_SHOWN;
    2) далі DC (_is_dc_station) за відстанню, доки не набереться MAX_DC_SHOWN
       (обмежено рештою загальної квоти);
    3) далі AC за відстанню, доки загалом не набереться MAX_STATIONS_TOTAL.

    Якщо DC у пулі менше за MAX_DC_SHOWN — решту вільної квоти добирають AC;
    якщо AC теж не вистачає заповнити те, що лишилось, — квоту добирають
    DC понад MAX_DC_SHOWN. Мета — видача не повинна ставати біднішою, ніж
    дозволяє реальний пул кандидатів, навіть коли один із типів рідкісний.

    Порядок результату: [оператори] + [DC] + [AC] — НЕ пересортовується за
    відстанню вкінці, інакше далека операторська станція могла б
    "провалитись" за ближчі DC/AC, хоча має лишатись першою. НАСЛІДОК:
    усередині DC-блоку й AC-блоку порядок за відстанню зберігається, але
    МІЖ блоками — ні, тому DC-станція може стояти вище в списку за AC-
    станцію, яка формально ближча до водія. Це свідомий вибір на користь
    швидких станцій (сама причина цього бандла), а не побічний ефект.
    """
    operators = [it for it in items if it["source"] == "operator"][:MAX_OPERATOR_SHOWN]
    dc = [it for it in items if it["source"] != "operator" and _is_dc_station(it["station"])]
    ac = [it for it in items if it["source"] != "operator" and not _is_dc_station(it["station"])]

    budget = MAX_STATIONS_TOTAL - len(operators)

    dc_take = min(len(dc), MAX_DC_SHOWN, budget)
    selected_dc = dc[:dc_take]
    budget -= dc_take

    ac_take = min(len(ac), budget)
    selected_ac = ac[:ac_take]
    budget -= ac_take

    if budget > 0:
        # AC не вистачило заповнити квоту -> добираємо DC понад MAX_DC_SHOWN.
        extra_dc = dc[dc_take:dc_take + budget]
        selected_dc += extra_dc
        budget -= len(extra_dc)

    if budget > 0:
        # DC теж вичерпані (рідкісний випадок: обидва пули малі) -> лишок AC.
        extra_ac = ac[ac_take:ac_take + budget]
        selected_ac += extra_ac

    return operators + selected_dc + selected_ac


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


def _format_tomtom_status_line(availability) -> str | None:
    """Короткий рядок «вільно X/Y» з нормалізованого get_availability(). None,
    якщо статус не тягнули (квота/помилка/немає id) або конекторів 0."""
    if not availability:
        return None
    total_available = sum(c.get("available", 0) for c in availability)
    total = sum(
        c.get("available", 0) + c.get("occupied", 0) + c.get("out_of_service", 0)
        for c in availability
    )
    if total == 0:
        return None
    return f"🟢 Вільно зараз: <b>{total_available}/{total}</b>"


def _format_tomtom_station_card(idx: int, station: dict) -> str:
    """
    Картка станції з живого інфошару TomTom — інформаційна (без QR/оплати
    через бота, водій платить на місці). Обов'язковий видимий підпис
    «© TomTom» — умова ліцензії на показ цих даних.
    """
    badge = classify_station_speed(station.get("power_kw"), station.get("connector_type"))
    prefix = f"{badge} " if badge else ""
    name = html.escape(station.get("name") or "Без назви")
    address = html.escape(station.get("address") or "Адреса не вказана")
    lines = [
        f"{prefix}<b>Станція #{idx}: {name}</b>",
        f"📍 {address}",
    ]
    distance = station.get("distance_km")
    if distance is not None:
        lines.append(f"📏 Відстань: <b>{distance:.2f} км</b>")
    if station.get("power_kw"):
        lines.append(f"⚙️ Потужність: {station['power_kw']} кВт")
    if station.get("connector_type"):
        lines.append(f"🔌 Конектор: {html.escape(station['connector_type'])}")
    status_line = _format_tomtom_status_line(station.get("availability"))
    if status_line:
        lines.append(status_line)
    lines.append("ℹ️ Інформаційна станція — оплата на місці, не через бот")
    lines.append(f"<i>{tomtom_service.ATTRIBUTION_TEXT}</i>")
    return "\n".join(lines)


async def _attach_tomtom_status(shown_items):
    """Дотягує live-статус ОДНИМ додатковим TomTom-викликом на станцію — і
    лише для TomTom-станцій, що реально потрапили у ФІНАЛЬНУ видачу (після
    злиття, дедупу й відбору _select_by_quota, MAX_STATIONS_TOTAL станцій
    щонайбільше), а не для всіх станцій, які взагалі повернув nearbySearch."""
    for item in shown_items:
        if item["source"] != "tomtom":
            continue
        availability_id = item["station"].get("charging_availability_id")
        if availability_id:
            item["station"]["availability"] = await tomtom_service.get_availability(availability_id)
    return shown_items


# ---------------------------------------------------------------------------
# ОДНЕ повідомлення замість N карток (SEARCH_SINGLE_MESSAGE, дефолт-режим).
# Старі _format_*_station_card / _format_tomtom_status_line вище лишаються
# НЕДОТОРКАНИМИ — вони обслуговують старий рендер за SEARCH_SINGLE_MESSAGE
# =false, прапорець-відкат без деплою.
# ---------------------------------------------------------------------------

MAX_FIELD_LEN = 60  # запобіжник ліміту Telegram 4096 символів на все повідомлення


def _truncate(text, max_len: int = MAX_FIELD_LEN) -> str:
    """Обрізає СИРИЙ текст (до html.escape) — щоб обрізання не розкраяло
    HTML-сутність на кшталт "&amp;" навпіл."""
    if not text:
        return ""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _format_operator_result_entry(idx: int, station: dict) -> str:
    """Один запис пронумерованої видачі — операторська станція. Оплата —
    кнопкою "⚡ Оплатити" в get_search_results_keyboard, не текстом-URL
    (як було в старому _format_operator_station_card)."""
    badge = classify_station_speed(station.get("power_kw"), station.get("connector_type"))
    prefix = f"{badge} " if badge else ""
    name = html.escape(_truncate(station["name"]))

    lines = [f"{idx}. {prefix}<b>{name}</b>"]

    meta = []
    if station.get("address"):
        meta.append(f"📍 {html.escape(_truncate(station['address']))}")
    meta.append(f"📏 {station['distance_km']:.2f} км")
    lines.append(" · ".join(meta))

    details = []
    if station.get("power_kw"):
        details.append(f"⚙️ {station['power_kw']} кВт")
    if station.get("connector_type"):
        details.append(f"🔌 {html.escape(station['connector_type'])}")
    details.append(f"💰 {station['tariff_uah_kwh']} грн/кВт·год")
    lines.append(" · ".join(details))

    return "\n".join(lines)


def _format_tomtom_result_entry(idx: int, station: dict) -> str:
    """Один запис пронумерованої видачі — станція TomTom (інформаційна,
    без кнопки оплати — водій платить на місці, у клавіатурі лише карти)."""
    badge = classify_station_speed(station.get("power_kw"), station.get("connector_type"))
    prefix = f"{badge} " if badge else ""
    name = html.escape(_truncate(station.get("name") or "Без назви"))

    lines = [f"{idx}. {prefix}<b>{name}</b>"]

    meta = []
    if station.get("address"):
        meta.append(f"📍 {html.escape(_truncate(station['address']))}")
    distance = station.get("distance_km")
    if distance is not None:
        meta.append(f"📏 {distance:.2f} км")
    if meta:
        lines.append(" · ".join(meta))

    details = []
    if station.get("power_kw"):
        details.append(f"⚙️ {station['power_kw']} кВт")
    if station.get("connector_type"):
        details.append(f"🔌 {html.escape(station['connector_type'])}")
    status_line = _format_tomtom_status_line(station.get("availability"))
    if status_line:
        # Компактна форма для однорядкового блоку деталей запису; повний
        # варіант "🟢 Вільно зараз: X/Y" лишається у старому per-card рендері.
        details.append(status_line.replace("Вільно зараз: ", ""))
    if details:
        lines.append(" · ".join(details))

    return "\n".join(lines)


def _format_ocm_result_entry(idx: int, station: dict) -> str:
    """Один запис пронумерованої видачі — станція OCM. Рядок з ID лишається
    — флоу "надішліть ID" (waiting_for_station_id) не чіпається цим бандлом."""
    badge = classify_station_speed(station.get("power_kw"), station.get("connector_type"))
    prefix = f"{badge} " if badge else ""
    name = html.escape(_truncate(station.get("name")))

    lines = [f"{idx}. {prefix}<b>{name}</b>"]

    meta = []
    if station.get("address"):
        meta.append(f"📍 {html.escape(_truncate(station['address']))}")
    meta.append(f"📏 {station['distance']:.2f} км")
    lines.append(" · ".join(meta))

    if station.get("connectors"):
        lines.append(f"🔌 {html.escape(_truncate(station['connectors'], 80))}")

    lines.append(f"👉 ID: <code>{html.escape(station['id'])}</code>")
    return "\n".join(lines)


def _format_search_result_entry(idx: int, item: dict) -> str:
    source = item["source"]
    if source == "operator":
        return _format_operator_result_entry(idx, item["station"])
    if source == "tomtom":
        return _format_tomtom_result_entry(idx, item["station"])
    return _format_ocm_result_entry(idx, item["station"])


def _build_attribution_footer(combined) -> str | None:
    """
    ОДИН підвал наприкінці повідомлення — умова ліцензій TomTom і Open
    Charge Map, не косметика. Перелічує ЛИШЕ джерела, реально присутні в
    ЦІЙ видачі: операторська видача власна (жодної зовнішньої ліцензії),
    тому їй підпис не потрібен.
    """
    sources_present = {item["source"] for item in combined}
    parts = []
    if "tomtom" in sources_present:
        parts.append(tomtom_service.ATTRIBUTION_TEXT)
    if "ocm" in sources_present:
        parts.append(OCM_ATTRIBUTION_TEXT)
    if not parts:
        return None
    return "<i>Джерела даних: " + " · ".join(parts) + "</i>"


def _format_search_results_message(combined) -> str:
    """
    ОДНЕ HTML-повідомлення замість N окремих карток — раніше цикл
    message.answer() на кожну станцію означав до MAX_STATIONS_TOTAL окремих
    повідомлень на один пошук, задовго гортати в чаті. Перший рядок —
    той самий лічильник "🎯 Знайдено N станцій поруч:", що був окремим
    повідомленням у старому рендері (SEARCH_SINGLE_MESSAGE=false), тепер
    перший рядок ЦЬОГО повідомлення. Далі пронумеровані записи по 3-4
    рядки (`_format_search_result_entry`), один спільний підвал з
    атрибуцією наприкінці.
    """
    header = f"🎯 **Знайдено {len(combined)} станцій поруч:**"
    entries = "\n\n".join(
        _format_search_result_entry(idx, item) for idx, item in enumerate(combined, 1)
    )
    footer = _build_attribution_footer(combined)
    parts = [header, entries]
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)


def _build_keyboard_items(combined):
    """Готує вхід для get_search_results_keyboard (app/keyboards/reply.py) —
    координати через _station_coords (уже враховує lng-ключ операторських
    станцій) і, для операторських, URL оплати. Станцію без розпізнаних
    координат пропускаємо — без них немає що покласти в кнопки карт."""
    items = []
    for idx, item in enumerate(combined, 1):
        coords = _station_coords(item["station"])
        if coords is None:
            continue
        lat, lon = coords
        pay_url = None
        if item["source"] == "operator":
            pay_url = f"{PUBLIC_BASE_URL}/s/{item['station']['qr_slug']}"
        items.append({"idx": idx, "lat": lat, "lon": lon, "pay_url": pay_url})
    return items


@router.message(F.location, StateFilter("*"))
async def handle_location(message: types.Message, state: FSMContext):
    await message.answer("🔍 **Шукаємо станції поруч...**")
    lat, lon = message.location.latitude, message.location.longitude

    # Джерела ОБ'ЄДНУЮТЬСЯ, а не заміщують одне одного (рев'ю живого смоуку
    # 31.07.2026: коли TomTom відповідав успішно, OCM не запитувався
    # взагалі — швидкі DC-станції, знані лише OCM, зникали з видачі). OCM
    # тепер запитується завжди, з ширшим пулом (OCM_SEARCH_MAXRESULTS), щоб
    # квоті нижче було з чого відбирати; TomTom, що технічно недоступний
    # (None — немає ключа/вичерпана квота/помилка), просто дає порожній шар
    # без окремого фолбеку — дублікатів об'єднання не створює, дедуп все
    # одно прибирає збіги.
    operator_stations = await op_repo.list_public_stations_near(lat, lon, OPERATOR_SEARCH_RADIUS_KM)
    ocm_stations = await find_three_nearest_stations(lat, lon, maxresults=OCM_SEARCH_MAXRESULTS) or []
    tomtom_stations = await tomtom_service.search_stations_near(
        lat, lon, TOMTOM_SEARCH_RADIUS_KM, TOMTOM_SEARCH_LIMIT
    ) or []

    merged = _merge_search_results(ocm_stations, operator_stations, tomtom_stations)
    deduped = _dedupe_by_proximity(merged)
    # Квота (оператор -> DC -> AC) діє в ОБОХ режимах рендеру нижче — це
    # відбір станцій, а не спосіб їх показу (SEARCH_SINGLE_MESSAGE).
    combined = _select_by_quota(deduped)

    if not combined:
        await message.answer("❌ Станцій поблизу не знайдено.")
        return

    await _attach_tomtom_status(combined)

    # Ручний запуск за ID (наступний крок FSM) працює лише для OCM-станцій —
    # операторську станцію водій оплачує через QR/посилання, а не введенням ID.
    if any(item["source"] == "ocm" for item in combined):
        await state.set_state(BotStates.waiting_for_station_id)

    if SEARCH_SINGLE_MESSAGE:
        text = _format_search_results_message(combined)
        keyboard = get_search_results_keyboard(_build_keyboard_items(combined))
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        return

    # SEARCH_SINGLE_MESSAGE=false — старий рендер, окрема картка на
    # станцію (прапорець-відкат без деплою, ПРАВКА 7). Формати карток і
    # get_single_station_keyboard НЕ чіпались цим бандлом.
    await message.answer(f"🎯 **Знайдено {len(combined)} станцій поруч:**")

    for idx, item in enumerate(combined, 1):
        station = item["station"]
        if item["source"] == "operator":
            await message.answer(_format_operator_station_card(idx, station), parse_mode="HTML")
        elif item["source"] == "tomtom":
            await message.answer(
                _format_tomtom_station_card(idx, station), parse_mode="HTML",
                reply_markup=get_single_station_keyboard(station["lat"], station["lon"]),
            )
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

async def _send_telegram_invoice(chat_id: int, package: str):
    """
    Старий тестовий флоу купівлі пакета через Telegram Payments — лишається
    лише за TELEGRAM_PAYMENTS_ENABLED (за замовчуванням вимкнено, див.
    process_successful_payment нижче). Поведінка не змінена.
    """
    payment_token = os.getenv("PAYMENT_PROVIDER_TOKEN")
    pkg = WALLET_PACKAGES[package]
    if package == "buy_pack_50":
        await bot.send_invoice(
            chat_id=chat_id,
            title="🔋 Пакет 50 кВт·год",
            description="Поповнення балансу мережі eVolt UA на 750 грн",
            payload="pack_50",
            provider_token=payment_token,
            currency="UAH",
            prices=[types.LabeledPrice(label="Пакет 50 кВт·год", amount=75000)]
        )
    else:
        await bot.send_invoice(
            chat_id=chat_id,
            title="🔥 Пакет 100 кВт·год",
            description="Поповнення балансу мережі eVolt UA на 1350 грн (Знижка 10%)",
            payload="pack_100",
            provider_token=payment_token,
            currency="UAH",
            prices=[types.LabeledPrice(label="Пакет 100 кВт·год", amount=135000)]
        )


async def _start_wallet_topup(chat_id: int, user_id: int, package: str):
    """
    Реальна купівля пакета: інвойс Monobank токеном оператора №0
    (WALLET_OPERATOR_ID), той самий еквайринг, що вже живий у станційному
    QR-флої (app/api/driver_qr.py). Нарахування kWh відбудеться пізніше,
    у webhook (app/api/wallet_webhook.py) — після того, як банк підтвердить
    оплату, а не з цього хендлера.
    """
    pkg = WALLET_PACKAGES[package]

    if WALLET_OPERATOR_ID <= 0:
        logging.error("Поповнення гаманця: WALLET_OPERATOR_ID не налаштований")
        await bot.send_message(chat_id, "⚠️ Поповнення тимчасово недоступне. Спробуйте пізніше.")
        return

    operator = await op_repo.get_operator(WALLET_OPERATOR_ID)
    if operator is None or operator["status"] != "active":
        logging.error(
            "Поповнення гаманця: оператор %s недоступний (status=%s)",
            WALLET_OPERATOR_ID, operator["status"] if operator else "не існує",
        )
        await bot.send_message(chat_id, "⚠️ Поповнення тимчасово недоступне. Спробуйте пізніше.")
        return

    token_encrypted = await op_repo.get_operator_monobank_token_encrypted(WALLET_OPERATOR_ID)
    if not token_encrypted:
        logging.error("Поповнення гаманця: немає збереженого еквайринг-токена оператора %s",
                      WALLET_OPERATOR_ID)
        await bot.send_message(chat_id, "⚠️ Поповнення тимчасово недоступне. Спробуйте пізніше.")
        return

    try:
        operator_token = decrypt_secret(token_encrypted)
    except (EncryptionKeyMissing, ValueError) as e:
        logging.error("Поповнення гаманця: не вдалося розшифрувати токен оператора %s: %s",
                      WALLET_OPERATOR_ID, e)
        await bot.send_message(chat_id, "⚠️ Тимчасова технічна проблема. Спробуйте за хвилину.")
        return

    bot_username = os.getenv("BOT_USERNAME")
    redirect_url = f"https://t.me/{bot_username}" if bot_username else PUBLIC_BASE_URL

    try:
        invoice = await create_invoice(
            operator_token,
            amount_uah=pkg["amount_uah"],
            reference=f"wallet-{pkg['code']}-{user_id}",
            redirect_url=redirect_url,
            webhook_url=f"{PUBLIC_BASE_URL}/webhook/wallet/{WALLET_OPERATOR_ID}",
            destination=f"Поповнення балансу eVolt: {pkg['title']}",
        )
    except MonobankError as e:
        logging.error("Поповнення гаманця: банк не створив інвойс для %s: %s", user_id, e)
        await bot.send_message(chat_id, "⚠️ Банк тимчасово недоступний. Спробуйте за хвилину.")
        return

    await op_repo.create_wallet_topup(
        WALLET_OPERATOR_ID, user_id, invoice["invoiceId"], pkg["code"], pkg["kwh"], pkg["amount_uah"],
    )

    logging.info("🧾 Поповнення гаманця: користувач %s, %s, інвойс %s на %s грн",
                user_id, package, invoice["invoiceId"], pkg["amount_uah"])

    await bot.send_message(
        chat_id,
        f"💳 <b>{html.escape(pkg['title'])}</b>\n\n"
        f"Сума: <b>{pkg['amount_uah']:.2f} грн</b>\n\n"
        f"Оплатіть за посиланням нижче — баланс поповниться автоматично одразу після оплати.",
        parse_mode="HTML",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="💳 Оплатити", url=invoice["pageUrl"])
        ]]),
    )


@router.callback_query(lambda c: c.data.startswith('buy_pack_') or c.data == 'activate_night', StateFilter("*"))
async def process_tariff_purchase(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.clear()
    action = callback_query.data
    chat_id = callback_query.message.chat.id

    if action in WALLET_PACKAGES:
        if TELEGRAM_PAYMENTS_ENABLED:
            await _send_telegram_invoice(chat_id, action)
        else:
            await _start_wallet_topup(chat_id, callback_query.from_user.id, action)
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
