"""
Тести на РЕАЛЬНИЙ продакшн-ендпоінт app/api/ocpi.py::receive_cdr.

Існуючий test_ocpi.py тестує НЕ цей ендпоінт, а окремий, ніде більше не
імпортований файл ocpi_emsp_cdrs_refactored.py (Pydantic-версія CDRRequest) —
осиротіла чернетка рефакторингу, яку ніхто не підключив до FastAPI app.
Через однакову назву тестового файлу це виглядало так, ніби receive_cdr
покритий тестами на успіх/дублікат, хоча насправді ці два шляхи в реальному
коді не перевірялись — лише авторизація (test_ocpi_auth.py). Цей файл
закриває саме ту прогалину, на реальній функції.

Запуск: pytest test_ocpi_cdr.py -v
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import app.api.ocpi as ocpi_module


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


async def test_receive_cdr_success_debits_energy_not_cost():
    """Новий CDR: записується в ocpi_cdrs і списується total_energy (кВт·год),
    а НЕ total_cost (грошова вартість у CPO) — ключовий фікс балансу."""
    mock_pool, mock_conn = _make_mock_pool(fetchval_return=None)  # ще не існує

    with patch.object(ocpi_module.connection, "db_pool", mock_pool), \
         patch.object(ocpi_module, "update_user_balance", new=AsyncMock()) as mock_update:
        response = await ocpi_module.receive_cdr(dict(VALID_CDR))

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
        response = await ocpi_module.receive_cdr(dict(VALID_CDR))

    assert response["status_message"] == "CDR already processed"
    mock_update.assert_not_awaited()


async def test_receive_cdr_rejects_negative_energy():
    """CDR з від'ємним total_energy має відхилятись 400, а не проходити як
    "списання" з від'ємним числом (раніше це фактично працювало як накрутка
    балансу — див. коментар у самому app/api/ocpi.py)."""
    bad_cdr = dict(VALID_CDR, total_energy=-5.0)
    with pytest.raises(HTTPException) as exc_info:
        await ocpi_module.receive_cdr(bad_cdr)
    assert exc_info.value.status_code == 400


async def test_receive_cdr_rejects_negative_cost():
    bad_cdr = dict(VALID_CDR, total_cost=-1.0)
    with pytest.raises(HTTPException) as exc_info:
        await ocpi_module.receive_cdr(bad_cdr)
    assert exc_info.value.status_code == 400


async def test_receive_cdr_rejects_missing_required_fields():
    for missing_field in ("id", "session_id", "auth_id"):
        bad_cdr = dict(VALID_CDR)
        bad_cdr.pop(missing_field)
        with pytest.raises(HTTPException) as exc_info:
            await ocpi_module.receive_cdr(bad_cdr)
        assert exc_info.value.status_code == 400, f"мало впасти без поля '{missing_field}'"


async def test_receive_cdr_fails_cleanly_without_db_pool():
    """Якщо пул підключень ще не ініціалізовано — акуратна 500 з поясненням,
    а не необроблений AttributeError на None.db_pool."""
    with patch.object(ocpi_module.connection, "db_pool", None):
        with pytest.raises(HTTPException) as exc_info:
            await ocpi_module.receive_cdr(dict(VALID_CDR))
    assert exc_info.value.status_code == 500
