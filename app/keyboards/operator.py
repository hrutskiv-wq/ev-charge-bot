"""Клавіатури кабінету оператора (Промпт 4)."""
from aiogram.utils.keyboard import InlineKeyboardBuilder


def get_cabinet_menu(has_token: bool):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔌 Мої станції", callback_data="opm:stations")
    builder.button(text="➕ Додати станцію", callback_data="opm:add_station")
    token_label = "💳 Еквайринг підключено" if has_token else "💳 Підключити еквайринг"
    builder.button(text=token_label, callback_data="opm:token")
    builder.button(text="💰 Виручка", callback_data="opm:revenue")
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
