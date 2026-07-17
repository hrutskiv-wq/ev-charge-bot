"""
Тести на РЕАЛЬНИЙ продакшн-ендпоінт app/api/ocpi.py::receive_cdr та на
Pydantic-модель app/schemas/ocpi.py::CDRRequest, якою він тепер типізований.

Раніше `receive_cdr` приймав нетипізований `cdr: dict` і валідував поля
вручну (окремі перевірки на відсутні поля й від'ємні total_energy/
total_cost, кожна з власним HTTPException). Тепер валідація — на рівні
Pydantic-моделі: FastAPI сам поверне 422 ще ДО виклику функції, якщо щось
не так. Тому тести на "погані" вхідні дані тепер перевіряють саме модель
CDRRequest (pydantic.ValidationError), а не HTTPException з receive_cdr.

Існуючий раніше test_ocpi.py тестував НЕ цей ендпоінт, а окремий, ніде
більше не імпортований файл ocpi_emsp_cdrs_refactored.py — осиротіла
чернетка, яку прибрано при інтеграції цієї моделі (вона повторювала два
вже виправлених баги: списання за total_cost замість total_energy, і
прямий запис у kw_transactions в обхід update_user_balance()).

Запуск: pytest test_ocpi_cdr.py -v
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import app.api.ocpi as ocpi_module
from app.schemas.ocpi import CDRRequest


def _make_mock_pool(fetchval_return=None):
    """Будує MagicMock-пул, що імітує asyncpg: pool.acquire() -> conn,
    conn.transaction() -> транзакція, обидва як async context manager'и."""
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = fetchval_return

    mock_transaction_cm = AsyncMock()
    mock_transaction_cm.__aenter__.return_value = None
    mock_transaction_cm.__aexit__.return_value = None
    mock_conn.transaction = MagicMock(return_value=mock_transaction_cm)

    mock_acquire_cm = AsyncMock()
    mock_acquire_cm.__aenter__.return_value = mock_conn
    mock_acquire_cm.__aexit__.return_value = None

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)
    return mock_pool, mock_conn


VALID_CDR = {
    "id": "CDR-123",
    "session_id": "SESS-1",
    "auth_id": "1",
    "total_energy": 10.5,
    "total_cost": 5.0,
}


# --- Тести на реальний ендпоінт receive_cdr ---

async def test_receive_cdr_success_debits_energy_not_cost():
    """Новий CDR: записується в ocpi_cdrs і списується total_energy (кВт·год),
    а НЕ total_cost (грошова вартість у CPO) — ключовий фікс балансу."""
    mock_pool, mock_conn = _make_mock_pool(fetchval_return=None)  # ще не існує

    with patch.object(ocpi_module.connection, "db_pool", mock_pool), \
         patch.object(ocpi_module, "update_user_balance", new=AsyncMock()) as mock_update:
        response = await ocpi_module.receive_cdr(CDRRequest(**VALID_CDR))

    assert response["status_code"] == 1000
    assert response["status_message"] == "Success"

    # Перевіряємо, що баланс списувався саме total_energy, а не total_cost
    mock_update.assert_awaited_once()
    _, kwargs = mock_update.call_args
    assert kwargs["amount_kwh"] == 10.5
    assert kwargs["user_id"] == 1
    assert kwargs["session_id"] == "SESS-1"


async def test_receive_cdr_duplicate_is_idempotent_and_skips_second_debit():
    """Той самий cdr_id вдруге — має повернути 'already processed' і НЕ
    викликати update_user_balance повторно (захист від подвійного списання)."""
    mock_pool, mock_conn = _make_mock_pool(fetchval_return="CDR-123")  # вже існує

    with patch.object(ocpi_module.connection, "db_pool", mock_pool), \
         patch.object(ocpi_module, "update_user_balance", new=AsyncMock()) as mock_update:
        response = await ocpi_module.receive_cdr(CDRRequest(**VALID_CDR))

    assert response["status_message"] == "CDR already processed"
    mock_update.assert_not_awaited()


async def test_receive_cdr_fails_cleanly_without_db_pool():
    """Якщо пул підключень ще не ініціалізовано — акуратна 500 з поясненням,
    а не необроблений AttributeError на None.db_pool."""
    from fastapi import HTTPException

    with patch.object(ocpi_module.connection, "db_pool", None):
        with pytest.raises(HTTPException) as exc_info:
            await ocpi_module.receive_cdr(CDRRequest(**VALID_CDR))
    assert exc_info.value.status_code == 500


# --- Тести на саму модель CDRRequest (валідація "з коробки") ---

def test_cdr_request_rejects_negative_energy():
    """CDR з від'ємним total_energy не може бути легітимним і раніше міг
    використовуватись для накрутки балансу (withdrawal з від'ємною сумою
    ставав поповненням) — тепер це відхиляється на рівні моделі, до того,
    як запит взагалі дістанеться коду ендпоінту."""
    with pytest.raises(ValidationError):
        CDRRequest(**dict(VALID_CDR, total_energy=-5.0))


def test_cdr_request_rejects_negative_cost():
    with pytest.raises(ValidationError):
        CDRRequest(**dict(VALID_CDR, total_cost=-1.0))


def test_cdr_request_rejects_non_positive_auth_id():
    """auth_id <= 0 безглуздий як Telegram user_id. Стара ручна перевірка
    `if not user_id` пропускала від'ємні значення (bool(-5) є True) — це
    прогалину закриває саме обмеження gt=0 на рівні моделі."""
    for bad_auth_id in (0, -1):
        with pytest.raises(ValidationError):
            CDRRequest(**dict(VALID_CDR, auth_id=bad_auth_id))


def test_cdr_request_rejects_missing_required_fields():
    for missing_field in ("id", "session_id", "auth_id"):
        bad_cdr = dict(VALID_CDR)
        bad_cdr.pop(missing_field)
        with pytest.raises(ValidationError):
            CDRRequest(**bad_cdr)


def test_cdr_request_rejects_empty_string_id_and_session_id():
    with pytest.raises(ValidationError):
        CDRRequest(**dict(VALID_CDR, id=""))
    with pytest.raises(ValidationError):
        CDRRequest(**dict(VALID_CDR, session_id=""))


def test_cdr_request_coerces_numeric_strings():
    """CPO присилає auth_id рядком ("1") — Pydantic сам приводить до int,
    так само, як раніше робив ручний int(cdr.get("auth_id", 0))."""
    cdr = CDRRequest(**VALID_CDR)
    assert cdr.auth_id == 1
    assert isinstance(cdr.auth_id, int)
