import logging
from fastapi import APIRouter, HTTPException, Header
from app.schemas.ocpi_tariff import Tariff
from app.database.connection import save_ocpi_tariff

router = APIRouter()

@router.post("/ocpi/2.2.1/tariffs/")
async def receive_tariff(tariff: Tariff, authorization: str = Header(None)):
    if not authorization or "Token" not in authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    logging.info(f"📥 Отримано оновлення тарифу {tariff.id} від партнерської мережі CPO")
    
    # Шукаємо компонент вартості електроенергії (ENERGY)
    energy_price = 0.0
    for element in tariff.elements:
        for component in element.price_components:
            if component.type.upper() == "ENERGY":
                energy_price = component.price
                break
                
    if energy_price == 0.0:
        logging.warning(f"⚠️ В отриманому тарифі {tariff.id} не знайдено ENERGY компоненту ціни.")
        
    # Зберігаємо собівартість у нашу PostgreSQL базу даних
    await save_ocpi_tariff(tariff_id=tariff.id, price=energy_price, currency=tariff.currency)
    
    return {"status_code": 1000, "status_message": "Tariff Accepted"}
