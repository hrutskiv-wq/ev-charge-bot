"""
Pydantic-моделі для OCPI-пейлоадів. Раніше `receive_cdr(cdr: dict)` приймав
нетипізований dict — валідація (обов'язкові поля, заборона від'ємних
значень) робилась вручну, рядок за рядком, у самому ендпоінті.

Ця модель виросла з окремого чернеткового файлу `ocpi_emsp_cdrs_refactored.py`
(був у репо, ніде не підключений — знайдено при аудиті git-історії
2026-07-17). Сама ідея Pydantic-моделі звідти хороша й перенесена сюди, але
БІЗНЕС-ЛОГІКА з того файлу — НІ: там сесія списувалась за `total_cost`
(грошова вартість), а не `total_energy` (кВт·год), і запис ішов напряму в
kw_transactions в обхід `update_user_balance()` — тобто відтворювала обидва
баги, які раніше вже виправлялись у `app/api/ocpi.py`. Реальна логіка
залишається виключно в `app/api/ocpi.py::receive_cdr`.
"""
from pydantic import BaseModel, Field


class CDRRequest(BaseModel):
    id: str = Field(..., min_length=1, description="Унікальний ID CDR від CPO")
    session_id: str = Field(..., min_length=1)
    auth_id: int = Field(..., gt=0, description="Telegram user_id водія")
    total_energy: float = Field(default=0.0, ge=0.0, description="Спожита енергія, кВт·год")
    total_cost: float = Field(default=0.0, ge=0.0, description="Вартість сесії у валюті CPO")

    # Примітка: Pydantic v2 у стандартному (lax) режимі сам приводить рядки
    # на кшталт "123" чи "12.5" до int/float — окремий кастомний валідатор
    # для цього не потрібен (раніше в застосунку це робилось вручну через
    # int(cdr.get(...))/float(cdr.get(...))).
