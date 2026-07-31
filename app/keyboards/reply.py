from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="Зарядка ⚡")
    builder.button(text="Баланс 💳")
    builder.button(text="Ваучер 🧾")
    builder.button(text="Online підтримка 📢")
    # "Як працює?" прибрано з меню — інструкція там була фактично
    # дублікатом кроків, які й так показує "Зарядка ⚡" по кліку.
    # Хендлер process_help_click лишається (спрацьовує з тексту/голосу),
    # просто не винесений окремою кнопкою.
    # Баланс і Ваучер (поповнення) в одному рядку поруч, як логічна пара
    # "перевірити скільки є / поповнити ще".
    # Кабінет оператора (Промпт 4) — окремим рядком унизу: стосується
    # невеликої частини користувачів (власників станцій), не змішуємо
    # з водійськими кнопками вище.
    builder.button(text="🏷️ Мій білінг")
    builder.adjust(1, 2, 1, 1)
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
    builder.adjust(1, 1, 1)
    return builder.as_markup()

def get_single_station_keyboard(lat, lon):  #
    builder = InlineKeyboardBuilder()
    # Виправили посилання на офіційний стандарт Google Maps:
    builder.button(text="🗺 Google Maps", url=f"https://www.google.com/maps?q={lat},{lon}")
    builder.button(text="🚙 Waze", url=f"https://waze.com/ul?ll={lat},{lon}&navigate=yes")
    builder.adjust(2)
    return builder.as_markup()

def get_search_results_keyboard(items):
    """
    Клавіатура під ОДНИМ повідомленням видачі пошуку станцій
    (feature/search-results-single-message) — один рядок на станцію: карти
    (Google Maps + Waze, ті самі URL-формати, що вже в
    get_single_station_keyboard — не вигадуємо нові) з координатами САМЕ
    цієї станції, і — коли задано pay_url — третя кнопка оплати в ТОМУ Ж
    рядку.

    `items`: [{"idx": int, "lat": float, "lon": float, "pay_url": str|None}, ...],
    номер (idx) відповідає нумерації записів у тексті повідомлення.
    Формується в app/handlers/user.py — ця функція нічого не знає про
    джерела станцій чи дедуп, лише про готові координати/URL.
    """
    builder = InlineKeyboardBuilder()
    for item in items:
        idx = item["idx"]
        lat, lon = item["lat"], item["lon"]
        row = [
            InlineKeyboardButton(text=f"🗺 {idx}", url=f"https://www.google.com/maps?q={lat},{lon}"),
            InlineKeyboardButton(text=f"🚙 {idx}", url=f"https://waze.com/ul?ll={lat},{lon}&navigate=yes"),
        ]
        if item.get("pay_url"):
            row.append(InlineKeyboardButton(text="⚡ Оплатити", url=item["pay_url"]))
        builder.row(*row)
    return builder.as_markup()


def get_connectors_keyboard(connectors_string):  #
    builder = InlineKeyboardBuilder()
    connectors_list = connectors_string.split("; ")
    for conn in connectors_list:
        if conn.strip() and conn != "Інформація відсутня":
            builder.button(text=f"🔌 Увімкнути {conn}", callback_data=f"select_conn:{conn[:30]}")
    builder.adjust(1)
    return builder.as_markup()