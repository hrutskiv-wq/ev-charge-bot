import pytest
from unittest.mock import AsyncMock, MagicMock
from ocpi_emsp_cdrs_refactored import receive_cdr, CDRRequest

@pytest.mark.asyncio
async def test_receive_cdr_success(mocker):
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    
    # Ваш метод мокінгу транзакції
    mock_transaction_cm = MagicMock()
    mock_transaction_cm.__aenter__ = AsyncMock(return_value=None)
    mock_transaction_cm.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_transaction_cm)
    
    mocker.patch("ocpi_emsp_cdrs_refactored.connection.db_pool", mock_pool)
    mock_conn.fetchval.return_value = None 
    
    cdr_data = CDRRequest(id="CDR-123", session_id="SESS-1", auth_id=1, total_energy=10.5, total_cost=5.0)
    response = await receive_cdr(cdr_data)
    
    assert response["status_code"] == 1000
    assert mock_conn.execute.called

@pytest.mark.asyncio
async def test_receive_cdr_duplicate(mocker):
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    
    # Ваш метод мокінгу транзакції
    mock_transaction_cm = MagicMock()
    mock_transaction_cm.__aenter__ = AsyncMock(return_value=None)
    mock_transaction_cm.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=mock_transaction_cm)
    
    mocker.patch("ocpi_emsp_cdrs_refactored.connection.db_pool", mock_pool)
    mock_conn.fetchval.return_value = 1
    
    cdr_data = CDRRequest(id="CDR-123", session_id="SESS-1", auth_id=1, total_energy=10.5, total_cost=5.0)
    response = await receive_cdr(cdr_data)
    
    assert response["status_message"] == "CDR already processed"
    assert not mock_conn.execute.called
