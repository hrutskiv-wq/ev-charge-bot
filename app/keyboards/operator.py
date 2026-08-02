"""Клавіатури кабінету оператора (Промпт 4)."""
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Пресети конектора в майстрі станції (Промпт 4c) — оператори переважно
# повільні AC, тому саме ці типи покривають більшість випадків одним кліком.
CONNECTOR_PRESETS = ["Type 2", "GB/T AC", "Schuko", "CEE 5-pin (3ф)"]
CONNECTOR_OTHER_CALLBACK = "opconn:__other__"


def get_cabinet_menu(has_token: bool, show_checklist: bool = False, is_suspended: bool = False):
    """
    show_checklist — лише поки status == 'pending' (самообслуговуваний онбординг).

    is_suspended ховає МУТУЮЧІ кнопки («➕ Додати станцію», підключення/
    перевірка токена) — призупинений оператор і так отримає відмову гарда
    _is_suspended у самих хендлерах, але без цього він спершу тицяє кнопку,
    що гарантовано відмовить, замість одразу бачити реальний стан. «🔌 Мої
    станції» і «💰 Виручка» лишаються — перегляд для призупиненого свідомо
    відкритий (рішення бандла self-service-onboarding, підтверджене живим
    смоуком 28.07.2026).
    """
    builder = InlineKeyboardBuilder()
    if show_checklist:
        builder.button(text="📋 Прогрес активації", callback_data="opm:checklist")
    builder.button(text="🔌 Мої станції", callback_data="opm:stations")
    if not is_suspended:
        builder.button(text="➕ Додати станцію", callback_data="opm:add_station")
        token_label = "💳 Еквайринг підключено" if has_token else "💳 Підключити еквайринг"
        builder.button(text=token_label, callback_data="opm:token")
    builder.button(text="💰 Виручка", callback_data="opm:revenue")
    builder.adjust(1)
    return builder.as_markup()


def get_checklist_keyboard(checklist):
    """
    Кнопка на кожен незакритий критерій: немає токена взагалі -> підключити;
    є, але ще не підтверджений банком -> повторна перевірка (БЕЗ повторного
    вводу токена). Обидва пункти можуть бути закриті одночасно.
    """
    builder = InlineKeyboardBuilder()
    if not checklist.has_token:
        builder.button(text="💳 Підключити еквайринг", callback_data="opm:token")
    elif not checklist.token_verified:
        builder.button(text="🔁 Перевірити токен ще раз", callback_data="opm:verify_token")
    if not checklist.has_station:
        builder.button(text="➕ Додати станцію", callback_data="opm:add_station")
    builder.button(text="⬅️ Кабінет", callback_data="opm:home")
    builder.adjust(1)
    return builder.as_markup()


def get_station_list_keyboard(stations):
    """Один рядок-кнопка на станцію -> детальна картка з діями."""
    builder = InlineKeyboardBuilder()
    for station in stations:
        icon = "🟢" if station["status"] == "active" else "⚪"
        builder.button(text=f"{icon} {station['name']}", callback_data=f"opst:{station['id']}:view")
    builder.button(text="⬅️ Кабінет", callback_data="opm:home")
    builder.adjust(1)
    return builder.as_markup()


def get_station_detail_keyboard(station_id: int, status: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Змінити тариф", callback_data=f"opst:{station_id}:tariff")
    toggle_label = "⏸ Вимкнути" if status == "active" else "▶️ Увімкнути"
    builder.button(text=toggle_label, callback_data=f"opst:{station_id}:toggle")
    builder.button(text="🖼 Надіслати QR ще раз", callback_data=f"opst:{station_id}:qr")
    builder.button(text="⬅️ Мої станції", callback_data="opm:stations")
    builder.adjust(1)
    return builder.as_markup()


def get_revenue_period_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Сьогодні", callback_data="oprev:today")
    builder.button(text="Тиждень", callback_data="oprev:week")
    builder.button(text="Місяць", callback_data="oprev:month")
    builder.button(text="⬅️ Кабінет", callback_data="opm:home")
    builder.adjust(3, 1)
    return builder.as_markup()


def get_revenue_csv_keyboard(period: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="📄 Вивантажити CSV", callback_data=f"opcsv:{period}")
    builder.button(text="⬅️ Кабінет", callback_data="opm:home")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_activation_keyboard(operator_id: int):
    """
    Кнопка під повідомленням про нового оператора в LOGS_CHAT_ID. Ручний
    запасний шлях — самообслуговуваний онбординг активує оператора
    автоматично, ця кнопка лишається для форсованої негайної активації.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Активувати", callback_data=f"opadm:activate:{operator_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_suspend_keyboard(operator_id: int):
    """Кнопка під сповіщенням про автоактивацію — головний запобіжник самообслуговування."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🚫 Призупинити", callback_data=f"opadm:suspend:{operator_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_connector_presets_keyboard():
    """Крок конектора майстра станції: пресети + «Інше» (веде до вільного тексту)."""
    builder = InlineKeyboardBuilder()
    for preset in CONNECTOR_PRESETS:
        builder.button(text=preset, callback_data=f"opconn:{preset}")
    builder.button(text="Інше", callback_data=CONNECTOR_OTHER_CALLBACK)
    builder.adjust(1)
    return builder.as_markup()
