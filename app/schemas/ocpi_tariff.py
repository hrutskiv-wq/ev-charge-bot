from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class PriceComponent(BaseModel):
    type: str  # ENERGY (за кВт·год), TIME (за хвилину), FLAT (за старт)
    price: float  # Ціна без ПДВ
    step_size: int  # Мінімальний крок (наприклад, 1000 Вт або 60 секунд)

class TariffElements(BaseModel):
    price_components: List[PriceComponent]

class Tariff(BaseModel):
    id: str  # Унікальний ID тарифу від мережі
    currency: str = "UAH"
    elements: List[TariffElements]
    last_updated: datetime
