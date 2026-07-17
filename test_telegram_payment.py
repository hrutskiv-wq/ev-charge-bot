"""
Тести на app/handlers/user.py::process_successful_payment.

До фіксу цей хендлер писав напряму (UPDATE users SET balance ... + INSERT
INTO kw_transactions), в обхід і update_user_balance(), і таблиці payments —
платежі через Telegram Invoice взагалі не залишали сліду в payments, тому
reconcile_payments.py не міг би їх перевірити. Тепер: спершу пишеться
payments (provider='telegram', invoice_id=telegram_payment_charge_id),
потім update_user_balance() з прив'язкою payment_id.

Запуск: pytest test_telegram_payment.py -v
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.handlers.user as user_module


def _make_message(payload="pack_50", total_amount=75000, charge_id="tg-charge-1"):
    successful_payment = SimpleNamespace(
        invoice_payload=payload,
        total_amount=total_amount,
        telegram_payment_charge_id=charge_id,
        provider_payment_charge_id="provider-charge-1",
        currency="UAH",
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=555),
        successful_payment=successful_payment,
        answer=AsyncMock(),
    )
    return message


def _make_mock_pool(existing_payment=None, new_payment_id=42):
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = existing_payment
    mock_conn.fetchval.return_value = new_payment_id

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


async def test_new_payment_creates_payments_row_and_credits_via_update_user_balance():
    message = _make_message(payload="pack_50", total_amount=75000, charge_id="tg-1")
    mock_pool, mock_conn = _make_mock_pool(existing_payment=None, new_payment_id=42)

    with patch.object(user_module.db_conn, "db_pool", mock_pool), \
         patch.object(user_module.db_conn, "update_user_balance", new=AsyncMock()) as mock_update:
        await user_module.process_successful_payment(message)

    # Перевірка на дублікат за telegram_payment_charge_id
    mock_conn.fetchrow.assert_awaited_once()
    dup_check_args = mock_conn.fetchrow.call_args.args
    assert dup_check_args[1] == "tg-1"

    # Запис у payments
    mock_conn.fetchval.assert_awaited_once()
    insert_query, insert_args = mock_conn.fetchval.call_args.args[0], mock_conn.fetchval.call_args.args[1:]
    assert "INSERT INTO payments" in insert_query
    assert "'telegram'" in insert_query
    assert insert_args[1] == "tg-1"  # invoice_id
    assert insert_args[2] == 750.0  # 75000 копійок -> 750 грн

    # Нарахування кВт·год через єдину точку запису балансу
    mock_update.assert_awaited_once()
    _, kwargs = mock_update.call_args
    assert kwargs["user_id"] == 555
    assert kwargs["amount_kwh"] == 50.0
    assert kwargs["t_type"] == "deposit"
    assert kwargs["payment_id"] == 42

    message.answer.assert_awaited_once()


async def test_duplicate_telegram_payment_is_idempotent():
    """Той самий telegram_payment_charge_id вдруге — не повинен нараховувати повторно."""
    message = _make_message(charge_id="tg-dup")
    mock_pool, mock_conn = _make_mock_pool(existing_payment={"id": 1})

    with patch.object(user_module.db_conn, "db_pool", mock_pool), \
         patch.object(user_module.db_conn, "update_user_balance", new=AsyncMock()) as mock_update:
        await user_module.process_successful_payment(message)

    mock_update.assert_not_awaited()
    mock_conn.fetchval.assert_not_awaited()  # не мало дійти до INSERT INTO payments


async def test_pack_100_credits_100_kwh():
    message = _make_message(payload="pack_100", total_amount=135000, charge_id="tg-2")
    mock_pool, mock_conn = _make_mock_pool(existing_payment=None, new_payment_id=7)

    with patch.object(user_module.db_conn, "db_pool", mock_pool), \
         patch.object(user_module.db_conn, "update_user_balance", new=AsyncMock()) as mock_update:
        await user_module.process_successful_payment(message)

    _, kwargs = mock_update.call_args
    assert kwargs["amount_kwh"] == 100.0
