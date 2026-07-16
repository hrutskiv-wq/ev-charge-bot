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
            evse_uid = conn.get('evse_uid', 'EVSE1')
            conn_id = conn.get('connector_id', '1')
            
            # Створюємо красиві назви роз'ємів для водіїв
            display_name = "CCS 2 (DC Швидка ⚡)" if "CCS" in standard else "Type 2 (AC Повільна 🔌)"
            
            # Формат callback_data: ocpi_st_LOCATION_ID:EVSE_UID:CONNECTOR_ID
            callback_data = f"ocpi_st_{station_id}:{evse_uid}:{conn_id}"
            
            keyboard.append([
                InlineKeyboardButton(
                    text=f"🔌 Запустити {display_name}", 
                    callback_data=callback_data
                )
            ])
    else:
        # Дефолтний варіант, якщо конекторів немає в БД
        keyboard.append([
            InlineKeyboardButton(text="⚡ Запустити зарядку", callback_data=f"ocpi_st_{station_id}:EVSE1:1")
        ])
        
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
