"""
Тести на app/handlers/user.py::_build_balance_and_history_text — спільну
функцію форматування балансу й останніх 5 ledger-операцій, яку тепер
викликають і кнопка "Баланс 💳" (process_balance_click), і команда
/history (cmd_history), щоб не дублювати формат балансу втретє.

Заразом перевіряє фікс реального бага: до цього тесту 'refund' у списку
операцій показувався б як "-" списання з підписом "Зарядка/Витрата" —
той самий блок коду вважав "не-deposit" типом автоматично витратою, хоча
'refund' (доданий раніше цієї сесії) — це нарахування користувачу.

Запуск: pytest test_balance_history_display.py -v
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import app.database.connection as connection
from app.handlers.user import _build_balance_and_history_text


def _make_mock_pool(balance=40.3, discount=0.0, ledger_rows=None):
    """
    Той самий патерн мокання asyncpg-пулу, що й у test_ocpi_cdr.py:
    pool.acquire() -> conn (async context manager), conn.fetchrow/fetch/execute
    повертають наперед задані значення.
    """
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = "OK"  # INSERT users ... ON CONFLICT
    mock_conn.fetchrow.return_value = {"balance": balance, "discount": discount}
    mock_conn.fetch.return_value = ledger_rows or []

    mock_acquire_cm = AsyncMock()
    mock_acquire_cm.__aenter__.return_value = mock_conn
    mock_acquire_cm.__aexit__.return_value = None

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)
    return mock_pool, mock_conn


async def test_empty_history_shows_only_balance():
    mock_pool, _ = _make_mock_pool(balance=40.3, ledger_rows=[])
    with patch.object(connection, "db_pool", mock_pool):
        text = await _build_balance_and_history_text(user_id=1)

    assert "40.30" in text
    assert "Історія операцій порожня" in text


async def test_deposit_row_shows_plus_sign_and_top_up_label():
    rows = [{"amount": 50.0, "type": "deposit", "created_at": datetime.now(timezone.utc)}]
    mock_pool, _ = _make_mock_pool(ledger_rows=rows)
    with patch.object(connection, "db_pool", mock_pool):
        text = await _build_balance_and_history_text(user_id=1)

    assert "+50.00 кВт·год" in text
    assert "Поповнення" in text


async def test_withdrawal_row_shows_minus_sign_and_expense_label():
    rows = [{"amount": -12.0, "type": "ocpi_session", "created_at": datetime.now(timezone.utc)}]
    mock_pool, _ = _make_mock_pool(ledger_rows=rows)
    with patch.object(connection, "db_pool", mock_pool):
        text = await _build_balance_and_history_text(user_id=1)

    assert "-12.00 кВт·год" in text
    assert "Зарядка/Витрата" in text


async def test_refund_row_shows_plus_sign_not_minus():
    """Ключовий регресійний тест на знайдений баг: 'refund' — кредит,
    має показуватись з '+' і власним підписом, а не як звичайна витрата."""
    rows = [{"amount": 5.0, "type": "refund", "created_at": datetime.now(timezone.utc)}]
    mock_pool, _ = _make_mock_pool(ledger_rows=rows)
    with patch.object(connection, "db_pool", mock_pool):
        text = await _build_balance_and_history_text(user_id=1)

    assert "+5.00 кВт·год" in text
    assert "Повернення коштів" in text
    assert "-5.00 кВт·год" not in text
