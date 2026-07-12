from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def get_station_keyboard(station_id: str):
    """Генерує кнопки для взаємодії зі станцією OCPI"""
    keyboard = [
        [
            InlineKeyboardButton(text="🔄 Оновити статус", callback_data=f"ocpi_refresh_{station_id}")
        ],
        [
            InlineKeyboardButton(text="⚡ Запустити зарядку", callback_data=f"ocpi_start_{station_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
