"""
Тести на reconcile_payments.py. Не піднімають реальну Postgres — pool/conn
підмінені моками, що повертають наперед задані "рядки" (звичайні dict,
достатньо для доступу за row["key"], як у asyncpg.Record).

Запуск: pytest test_reconcile_payments.py -v
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from reconcile_payments import (
    _expected_kwh_for_payment,
    find_paid_but_not_credited,
    find_credited_without_valid_payment,
    find_amount_mismatches,
)


def test_expected_kwh_for_known_package_50():
    assert _expected_kwh_for_payment(750.0) == 50.0


def test_expected_kwh_for_known_package_100_with_discount():
    """1350 грн -> 100 кВт·год зі знижкою — НЕ 1350/15=90, а фіксований пакет."""
    assert _expected_kwh_for_payment(1350.0) == 100.0


def test_expected_kwh_for_custom_amount_uses_price_per_kwh():
    # 300 грн / 15 грн за кВт·год = 20 кВт·год (нетиповий, "ручний" платіж)
    assert _expected_kwh_for_payment(300.0) == 20.0


def _make_pool(fetch_return):
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = fetch_return

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_conn
    mock_cm.__aexit__.return_value = None

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_cm)
    return mock_pool, mock_conn


async def test_find_paid_but_not_credited_returns_rows():
    fake_rows = [{
        "id": 1, "user_id": 42, "invoice_id": "abc", "provider": "monobank",
        "amount": 750.0, "created_at": datetime.now(timezone.utc),
    }]
    pool, conn = _make_pool(fake_rows)

    result = await find_paid_but_not_credited(pool, datetime.now(timezone.utc))

    assert result == fake_rows
    conn.fetch.assert_awaited_once()


async def test_find_credited_without_valid_payment_returns_rows():
    fake_rows = [{
        "id": 5, "user_id": 7, "payment_id": 99, "amount": 10.0,
        "created_at": datetime.now(timezone.utc),
    }]
    pool, conn = _make_pool(fake_rows)

    result = await find_credited_without_valid_payment(pool, datetime.now(timezone.utc))

    assert result == fake_rows


async def test_find_amount_mismatches_flags_discrepancy():
    """Заплачено за пакет 50 кВт·год (750 грн), але нараховано лише 10 — розбіжність."""
    fake_rows = [{
        "payment_id": 1, "user_id": 1, "invoice_id": "x",
        "paid_uah": 750.0, "tx_id": 1, "credited_kwh": 10.0,
    }]
    pool, _ = _make_pool(fake_rows)

    mismatches = await find_amount_mismatches(pool, datetime.now(timezone.utc))

    assert len(mismatches) == 1
    row, expected_kwh, actual_kwh = mismatches[0]
    assert expected_kwh == 50.0
    assert actual_kwh == 10.0


async def test_find_amount_mismatches_no_discrepancy_for_correct_package():
    fake_rows = [{
        "payment_id": 1, "user_id": 1, "invoice_id": "x",
        "paid_uah": 1350.0, "tx_id": 1, "credited_kwh": 100.0,
    }]
    pool, _ = _make_pool(fake_rows)

    mismatches = await find_amount_mismatches(pool, datetime.now(timezone.utc))

    assert mismatches == []
