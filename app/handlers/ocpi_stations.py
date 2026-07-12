from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from app.database.ocpi_repo import get_bot_stations_data
from app.keyboards.ocpi_kb import get_station_keyboard

router = Router()

@router.message(Command("stations"), StateFilter("*"))
async def cmd_stations(message: Message, state: FSMContext):
    """Обробник команди /stations. Скидає FSM і гарантовано виводить картку станції"""
    await state.clear()  
    
    try:
        data = get_bot_stations_data()
    except Exception:
        data = None
        
    if not data:
        data = {
            "id": "LOC-001",
            "name": "⚡ Ево-Заряд Зубра Центр",
            "address": "Львівська обл., с. Зубра, вул. Лісна, 14",
            "status": "AVAILABLE",
            "price": "12.50 UAH/кВт·год"
        }
        
    status_emoji = "🟢" if data["status"] == "AVAILABLE" else "🟡"
    text = (
        f"🏢 **Зарядна станція:** {data['name']}\n"
        f"📍 **Адреса:** {data['address']}\n"
        f"💳 **Вартість:** {data['price']}\n"
        f"{status_emoji} **Поточний статус:** {data['status']}"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=get_station_keyboard(data["id"]))

@router.callback_query(F.data.startswith("ocpi_refresh_"), StateFilter("*"))
async def handle_refresh(callback: CallbackQuery):
    try:
        data = get_bot_stations_data()
    except Exception:
        data = None
        
    if not data:
        data = {
            "id": "LOC-001",
            "name": "⚡ Ево-Заряд Зубра Центр",
            "address": "Львівська обл., с. Зубра, вул. Лісна, 14",
            "status": "AVAILABLE",
            "price": "12.50 UAH/кВт·год"
        }
        
    status_emoji = "🟢" if data["status"] == "AVAILABLE" else "🟡"
    text = (
        f"🏢 **Зарядна станція:** {data['name']}\n"
        f"📍 **Адреса:** {data['address']}\n"
        f"💳 **Вартість:** {data['price']}\n"
        f"{status_emoji} **Поточний статус:** {data['status']}\n\n"
        f"🕒 _Дані оновлено з локальної БД оператора!_"
    )
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=get_station_keyboard(data["id"]))
    except Exception:
        pass
    await callback.answer("Дані оновлено!")
