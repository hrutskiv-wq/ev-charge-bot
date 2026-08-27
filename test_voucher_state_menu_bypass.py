"""
Регресійний тест на реальний баг, знайдений уже вживу (2026-07-17):
app/handlers/user.py::process_text_voucher спрацьовує на БУДЬ-яке
повідомлення, поки FSM-стан користувача — BotStates.waiting_for_code
(бот "чекає код ваучера"). Фільтр хендлера — лише StateFilter, без
перевірки самого тексту, тому натискання будь-якої іншої кнопки меню
(Баланс, Зарядка, Ваучер, Online підтримка, Головне меню) у цьому стані
ковталось як "невірний код ваучера" замість переходу в потрібний розділ.

Тести перевіряють, що тепер такі натискання коректно делегуються у
відповідні хендлери, а не показують помилкове "Невірний код ваучера".

Запуск: pytest test_voucher_state_menu_bypass.py -v
"""
from unittest.mock import AsyncMock, MagicMock, patch

import app.database.connection as connection
from app.handlers.user import process_text_voucher


def _make_message(text):
    message = MagicMock()
    message.text = text
    message.from_user.id = 1
    message.answer = AsyncMock()
    return message


def _make_mock_pool():
    """Той самий патерн мокання пулу, що й в test_ocpi_cdr.py: conn.transaction()
    має бути звичайним (не async) методом, що повертає async context manager,
    інакше `async with conn.transaction():` падає з TypeError на AsyncMock."""
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = "OK"
    mock_conn.fetchrow.return_value = {"balance": 70.0, "discount": 0.0}
    mock_conn.fetch.return_value = []

    mock_transaction_cm = AsyncMock()
    mock_transaction_cm.__aenter__.return_value = None
    mock_transaction_cm.__aexit__.return_value = None
    mock_conn.transaction = MagicMock(return_value=mock_transaction_cm)

    mock_acquire_cm = AsyncMock()
    mock_acquire_cm.__aenter__.return_value = mock_conn
    mock_acquire_cm.__aexit__.return_value = None

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)
    return mock_pool


async def test_pressing_balance_button_during_code_entry_shows_balance_not_invalid_code():
    message = _make_message("Баланс 💳")
    state = AsyncMock()

    with patch.object(connection, "db_pool", _make_mock_pool()):
        await process_text_voucher(message, state)

    sent_text = message.answer.call_args[0][0]
    assert "Невірний код ваучера" not in sent_text
    assert "баланс" in sent_text.lower()


async def test_pressing_charge_button_during_code_entry_shows_charge_menu_not_invalid_code():
    message = _make_message("Зарядка ⚡")
    state = AsyncMock()

    with patch.object(connection, "db_pool", _make_mock_pool()):
        await process_text_voucher(message, state)

    sent_text = message.answer.call_args[0][0]
    assert "Невірний код ваучера" not in sent_text
    assert "пошуку станції" in sent_text.lower()


async def test_actually_invalid_code_still_shows_invalid_code_message():
    """Переконуємось, що фікс не зламав основну функцію: реально невірний
    код (не назва кнопки меню) досі коректно відхиляється."""
    message = _make_message("НЕІСНУЮЧИЙКОД123")
    state = AsyncMock()

    with patch.object(connection, "db_pool", _make_mock_pool()):
        await process_text_voucher(message, state)

    sent_text = message.answer.call_args[0][0]
    assert "Невірний код ваучера" in sent_text


async def test_former_hardcoded_code_is_now_rejected():
    """
    "VOLT100" більше не нараховує нічого.

    Цей тест раніше стверджував протилежне («Код прийнято») — він перевіряв
    поведінку, яку видалено 24.08.2026 разом із захардкодженим кодом
    (PROJECT_CONTEXT.md §6.3). Лишається інвертованим, а не прибраним, бо
    саме рядок `VOLT100` у тестах — єдине місце, з якого видно, що код колись
    існував і що його прибрали свідомо, а не загубили.

    Перевірка, що при цьому НІЧОГО не записано в базу, — на живому Postgres
    (`test_voucher_removed_live.py`): мок не має ані транзакції, ані стану,
    тому «не нарахувало» він довести не може.
    """
    message = _make_message("VOLT100")
    state = AsyncMock()

    with patch.object(connection, "db_pool", _make_mock_pool()):
        await process_text_voucher(message, state)

    sent_text = message.answer.call_args[0][0]
    assert "Невірний код ваучера" in sent_text
