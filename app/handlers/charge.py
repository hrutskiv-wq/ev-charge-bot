from aiogram import Router, F
from aiogram.types import Message

router = Router()
charge_router = router  # Експортуємо під обома назвами для сумісності

@router.message(F.text == "Зарядка ⚡️")
async def cmd_charge(message: Message):
    await message.answer("Меню зарядки запущено!")
