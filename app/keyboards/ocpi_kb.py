from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def get_station_keyboard(station_id: str, connectors: list = None, price_per_kwh: float = 12.50):
    """Генерує динамічні кнопки для кожного конектора станції OCPI"""
    keyboard = [
        [
            InlineKeyboardButton(text="🔄 Оновити статус", callback_data=f"ocpi_refresh_{station_id}")
        ]
    ]
    
    if connectors:
        for conn in connectors:
            standard = conn.get('standard', 'Type 2')
            # Створюємо красиві назви роз'ємів для водіїв
            display_name = "CCS 2 (DC Швидка ⚡)" if "CCS" in standard else "Type 2 (AC Повільна 🔌)"
            keyboard.append([
                InlineKeyboardButton(
                    text=f"🔌 Запустити {display_name}", 
                    callback_data=f"ocpi_start_{station_id}:{standard}:{price_per_kwh}"
                )
            ])
    else:
        # Дефолтний варіант, якщо конекторів немає в БД
        keyboard.append([
            InlineKeyboardButton(text="⚡ Запустити зарядку", callback_data=f"ocpi_start_{station_id}:Type 2:12.50")
        ])
        
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
