from pydantic import BaseModel
from datetime import datetime

class CDR(BaseModel):
    cdr_id: str
    start_date_time: datetime
    stop_date_time: datetime
    auth_id: str  # Твій Telegram ID користувача
    total_energy: float
    total_cost: float
    currency: str = "UAH"
