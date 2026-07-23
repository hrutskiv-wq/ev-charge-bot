"""
Тести кнопок "Пакет 50/100 кВт·год" (app/handlers/user.py):
process_tariff_purchase / _start_wallet_topup / _send_telegram_invoice.

За замовчуванням (TELEGRAM_PAYMENTS_ENABLED вимкнено) натискання кнопки
пакета створює РЕАЛЬНИЙ Monobank-інвойс токеном WALLET_OPERATOR_ID — той
самий еквайринг-клієнт, що вже живий у станційному QR-флої
(app/api/driver_qr.py). Нарахування kWh тут НЕ відбувається — це вебхук
(app/api/wallet_webhook.py, тести — test_wallet_topup.py); цей файл
перевіряє лише коректність СТВОРЕННЯ інвойсу й запису wallet_topups.

Той самий підхід підміни, що в test_telegram_payment.py: живого Telegram і
живої Postgres немає.

Запуск: pytest test_wallet_topup_purchase.py -v
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.user as user_module

WALLET_OPERATOR_ID = 1


def _make_callback(action="buy_pack_50", user_id=555, chat_id=555):
    return SimpleNamespace(
        data=action,
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            answer=AsyncMock(),
        ),
    )


@pytest.fixture
def wallet_ready(monkeypatch):
    """Оператор WALLET_OPERATOR_ID active з токеном; create_invoice/create_wallet_topup підмінені."""
    monkeypatch.setattr(user_module, "WALLET_OPERATOR_ID", WALLET_OPERATOR_ID)
    monkeypatch.setattr(user_module, "TELEGRAM_PAYMENTS_ENABLED", False)

    calls = {"create_invoice": None, "create_wallet_topup": None, "sent_messages": []}

    async def get_operator(operator_id):
        assert operator_id == WALLET_OPERATOR_ID
        return {"id": operator_id, "status": "active"}

    async def get_operator_monobank_token_encrypted(operator_id):
        return "encrypted-token-stub"

    def decrypt_secret(token_encrypted):
        assert token_encrypted == "encrypted-token-stub"
        return "plain-merchant-token"

    async def create_invoice(operator_token, amount_uah, reference, redirect_url,
                             webhook_url, destination=None):
        calls["create_invoice"] = {
            "operator_token": operator_token, "amount_uah": amount_uah,
            "reference": reference, "redirect_url": redirect_url,
            "webhook_url": webhook_url, "destination": destination,
        }
        return {"invoiceId": "inv-new-1", "pageUrl": "https://pay.monobank.ua/inv-new-1"}

    async def create_wallet_topup(operator_id, user_id, invoice_id, package, kwh, amount_uah):
        calls["create_wallet_topup"] = {
            "operator_id": operator_id, "user_id": user_id, "invoice_id": invoice_id,
            "package": package, "kwh": kwh, "amount_uah": amount_uah,
        }
        return 42

    async def send_message(chat_id, text, **kwargs):
        calls["sent_messages"].append({"chat_id": chat_id, "text": text, **kwargs})

    monkeypatch.setattr(user_module.op_repo, "get_operator", get_operator)
    monkeypatch.setattr(user_module.op_repo, "get_operator_monobank_token_encrypted",
                        get_operator_monobank_token_encrypted)
    monkeypatch.setattr(user_module, "decrypt_secret", decrypt_secret)
    monkeypatch.setattr(user_module, "create_invoice", create_invoice)
    monkeypatch.setattr(user_module.op_repo, "create_wallet_topup", create_wallet_topup)
    monkeypatch.setattr(user_module.bot, "send_message", send_message)

    return calls


# --- створення інвойсу й запис wallet_topups ------------------------------------

async def test_buy_pack_50_creates_monobank_invoice_with_correct_amount(wallet_ready):
    callback = _make_callback("buy_pack_50", user_id=555, chat_id=555)
    state = SimpleNamespace(clear=AsyncMock())

    await user_module.process_tariff_purchase(callback, state)

    invoice_call = wallet_ready["create_invoice"]
    assert invoice_call is not None
    assert invoice_call["operator_token"] == "plain-merchant-token"
    assert invoice_call["amount_uah"] == Decimal("750.00")
    assert invoice_call["reference"] == "wallet-pack_50-555"
    assert invoice_call["webhook_url"].endswith(f"/webhook/wallet/{WALLET_OPERATOR_ID}")


async def test_buy_pack_100_creates_monobank_invoice_with_correct_amount(wallet_ready):
    callback = _make_callback("buy_pack_100", user_id=555, chat_id=555)
    state = SimpleNamespace(clear=AsyncMock())

    await user_module.process_tariff_purchase(callback, state)

    invoice_call = wallet_ready["create_invoice"]
    assert invoice_call["amount_uah"] == Decimal("1350.00")


async def test_successful_invoice_creates_wallet_topup_row(wallet_ready):
    callback = _make_callback("buy_pack_50", user_id=555, chat_id=555)
    state = SimpleNamespace(clear=AsyncMock())

    await user_module.process_tariff_purchase(callback, state)

    topup_call = wallet_ready["create_wallet_topup"]
    assert topup_call == {
        "operator_id": WALLET_OPERATOR_ID, "user_id": 555, "invoice_id": "inv-new-1",
        "package": "pack_50", "kwh": 50.0, "amount_uah": Decimal("750.00"),
    }


@pytest.mark.parametrize("action,expected_code", [
    ("buy_pack_50", "pack_50"),
    ("buy_pack_100", "pack_100"),
])
async def test_stored_package_code_matches_db_check_constraint(
        wallet_ready, action, expected_code):
    """
    wallet_topups.package має CHECK (package IN ('pack_50', 'pack_100'))
    (migrations/versions/0012_wallet_topups.py) — регресія на конкретний
    баг, спійманий тестами під час розробки: раніше сюди йшов буквально
    callback_query.data ('buy_pack_50'/'buy_pack_100'), що впало б на
    CHECK constraint при першій же реальній покупці пакета.
    """
    callback = _make_callback(action)
    state = SimpleNamespace(clear=AsyncMock())

    await user_module.process_tariff_purchase(callback, state)

    assert wallet_ready["create_wallet_topup"]["package"] == expected_code


async def test_driver_gets_payment_link_button(wallet_ready):
    callback = _make_callback("buy_pack_50", user_id=555, chat_id=555)
    state = SimpleNamespace(clear=AsyncMock())

    await user_module.process_tariff_purchase(callback, state)

    assert len(wallet_ready["sent_messages"]) == 1
    sent = wallet_ready["sent_messages"][0]
    assert sent["chat_id"] == 555
    button = sent["reply_markup"].inline_keyboard[0][0]
    assert button.url == "https://pay.monobank.ua/inv-new-1"


# --- відмовостійкість: помилки не мають падати з винятком -----------------------

async def test_wallet_operator_not_configured_shows_error_without_crashing(monkeypatch):
    monkeypatch.setattr(user_module, "WALLET_OPERATOR_ID", 0)
    monkeypatch.setattr(user_module, "TELEGRAM_PAYMENTS_ENABLED", False)
    sent = []

    async def send_message(chat_id, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(user_module.bot, "send_message", send_message)

    callback = _make_callback("buy_pack_50")
    state = SimpleNamespace(clear=AsyncMock())
    await user_module.process_tariff_purchase(callback, state)

    assert len(sent) == 1
    assert "недоступне" in sent[0]


async def test_inactive_operator_shows_error(wallet_ready, monkeypatch):
    async def get_operator(operator_id):
        return {"id": operator_id, "status": "pending"}

    monkeypatch.setattr(user_module.op_repo, "get_operator", get_operator)

    callback = _make_callback("buy_pack_50")
    state = SimpleNamespace(clear=AsyncMock())
    await user_module.process_tariff_purchase(callback, state)

    assert wallet_ready["create_invoice"] is None
    assert len(wallet_ready["sent_messages"]) == 1
    assert "недоступне" in wallet_ready["sent_messages"][0]["text"]


async def test_missing_token_shows_error(wallet_ready, monkeypatch):
    async def get_operator_monobank_token_encrypted(operator_id):
        return None

    monkeypatch.setattr(user_module.op_repo, "get_operator_monobank_token_encrypted",
                        get_operator_monobank_token_encrypted)

    callback = _make_callback("buy_pack_50")
    state = SimpleNamespace(clear=AsyncMock())
    await user_module.process_tariff_purchase(callback, state)

    assert wallet_ready["create_invoice"] is None
    assert len(wallet_ready["sent_messages"]) == 1


async def test_bank_error_shows_error_without_crashing(wallet_ready, monkeypatch):
    from app.services.monobank_acquiring import MonobankError

    async def create_invoice(*args, **kwargs):
        raise MonobankError("bank is down")

    monkeypatch.setattr(user_module, "create_invoice", create_invoice)

    callback = _make_callback("buy_pack_50")
    state = SimpleNamespace(clear=AsyncMock())
    await user_module.process_tariff_purchase(callback, state)

    assert wallet_ready["create_wallet_topup"] is None
    assert len(wallet_ready["sent_messages"]) == 1
    assert "Банк" in wallet_ready["sent_messages"][0]["text"]


# --- фіче-прапорець: старий Telegram Payments флоу вимкнений за замовчуванням ---

async def test_telegram_payments_flow_is_off_by_default(wallet_ready, monkeypatch):
    send_invoice = AsyncMock()
    monkeypatch.setattr(user_module.bot, "send_invoice", send_invoice)

    callback = _make_callback("buy_pack_50")
    state = SimpleNamespace(clear=AsyncMock())
    await user_module.process_tariff_purchase(callback, state)

    send_invoice.assert_not_awaited()
    assert wallet_ready["create_invoice"] is not None


async def test_telegram_payments_flow_can_be_re_enabled_via_flag(wallet_ready, monkeypatch):
    monkeypatch.setattr(user_module, "TELEGRAM_PAYMENTS_ENABLED", True)
    send_invoice = AsyncMock()
    monkeypatch.setattr(user_module.bot, "send_invoice", send_invoice)

    callback = _make_callback("buy_pack_50")
    state = SimpleNamespace(clear=AsyncMock())
    await user_module.process_tariff_purchase(callback, state)

    send_invoice.assert_awaited_once()
    assert wallet_ready["create_invoice"] is None, "Старий флоу не має чіпати Monobank"


# --- activate_night лишається незачепленим ---------------------------------------

async def test_activate_night_is_unaffected_by_wallet_changes(wallet_ready, monkeypatch):
    discount_calls = []

    async def set_user_discount(user_id, discount):
        discount_calls.append((user_id, discount))

    monkeypatch.setattr(user_module, "set_user_discount", set_user_discount)

    callback = _make_callback("activate_night", user_id=555)
    state = SimpleNamespace(clear=AsyncMock())
    await user_module.process_tariff_purchase(callback, state)

    assert discount_calls == [(555, 0.85)]
    callback.message.answer.assert_awaited_once()
    assert wallet_ready["create_invoice"] is None
