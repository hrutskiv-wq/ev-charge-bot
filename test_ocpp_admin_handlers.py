"""
Тести на app/handlers/ocpp_admin.py — /ocpp_start і /ocpp_stop, викликані
НАПРЯМУ (той самий підхід, що test_operator_cabinet.py): фільтри aiogram
(Command/StateFilter) тут не задіяні, порядок роутерів перевіряється
окремо в test_ocpp_admin_router.py через справжній Dispatcher. Тут — лише
бізнес-логіка самого хендлера: гейт LOGS_CHAT_ID + приватний чат, парсинг
аргументів, маршрутизація статусів сервісу в повідомлення.

Запуск: pytest test_ocpp_admin_handlers.py -v
"""
from decimal import Decimal

import pytest

from app.handlers import ocpp_admin
from app.services import ocpp_charging

ADMIN_CHAT_ID = 900001
LOGS_CHAT_ID = str(ADMIN_CHAT_ID)
OPERATOR_A = 1
STATION_ID = 10
USER_ID = 555


class FakeChat:
    def __init__(self, chat_id, chat_type):
        self.id = chat_id
        self.type = chat_type


class FakeMessage:
    def __init__(self, text, chat_id=ADMIN_CHAT_ID, chat_type="supergroup"):
        self.text = text
        self.chat = FakeChat(chat_id, chat_type)
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self


@pytest.fixture(autouse=True)
def admin_chat_env(monkeypatch):
    monkeypatch.setenv("LOGS_CHAT_ID", LOGS_CHAT_ID)


# ---------------------------------------------------------------------------
# /ocpp_start — гейт
# ---------------------------------------------------------------------------

async def test_ocpp_start_ignored_outside_admin_chat(monkeypatch):
    called = []
    async def fake_start(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStartResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "start_charging_session", fake_start)

    message = FakeMessage("/ocpp_start 1 10 555 20.0", chat_id=999)  # не LOGS_CHAT_ID
    await ocpp_admin.cmd_ocpp_start(message)

    assert message.sent == [], "Поза адмін-чатом — тиша, без жодної відповіді"
    assert called == [], "Сервіс не мав викликатись"


async def test_ocpp_start_works_in_admin_supergroup(monkeypatch):
    """
    LOGS_CHAT_ID на проді — супергрупа (-100...), не приватний чат
    засновника: гейт має пропускати команду САМЕ звідти (той самий чат, що
    admin_activate_operator у operator_billing.py, без перевірки на
    приватність).
    """
    called = []
    async def fake_start(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStartResult(status="ok", reservation_id=1, id_tag="x")
    monkeypatch.setattr(ocpp_charging, "start_charging_session", fake_start)

    message = FakeMessage("/ocpp_start 1 10 555 20.0", chat_id=ADMIN_CHAT_ID, chat_type="supergroup")
    await ocpp_admin.cmd_ocpp_start(message)

    assert len(called) == 1, "chat_id збігається з LOGS_CHAT_ID — команда мала пройти навіть у супергрупі"
    assert len(message.sent) == 1


# ---------------------------------------------------------------------------
# /ocpp_start — happy path, валідація, статуси
# ---------------------------------------------------------------------------

async def test_ocpp_start_happy_path_calls_service_and_reports_ok(monkeypatch):
    calls = []
    async def fake_start(operator_id, station_id, user_id, reserved_kwh):
        calls.append((operator_id, station_id, user_id, reserved_kwh))
        return ocpp_charging.ChargingStartResult(status="ok", reservation_id=42, id_tag="tag123456789012")
    monkeypatch.setattr(ocpp_charging, "start_charging_session", fake_start)

    message = FakeMessage(f"/ocpp_start {OPERATOR_A} {STATION_ID} {USER_ID} 20.0")
    await ocpp_admin.cmd_ocpp_start(message)

    assert calls == [(OPERATOR_A, STATION_ID, USER_ID, Decimal("20.0"))]
    assert len(message.sent) == 1
    assert "#42" in message.sent[0][0]


async def test_ocpp_start_bad_args_count_gives_friendly_error_no_side_effects(monkeypatch):
    called = []
    async def fake_start(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStartResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "start_charging_session", fake_start)

    message = FakeMessage("/ocpp_start 1 10")
    await ocpp_admin.cmd_ocpp_start(message)

    assert called == []
    assert len(message.sent) == 1
    assert "Використання" in message.sent[0][0]


async def test_ocpp_start_bad_number_format_gives_friendly_error_no_side_effects(monkeypatch):
    called = []
    async def fake_start(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStartResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "start_charging_session", fake_start)

    message = FakeMessage("/ocpp_start abc 10 555 20.0")
    await ocpp_admin.cmd_ocpp_start(message)

    assert called == []
    assert "Биті аргументи" in message.sent[0][0]


async def test_ocpp_start_non_positive_reserved_kwh_rejected_no_side_effects(monkeypatch):
    called = []
    async def fake_start(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStartResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "start_charging_session", fake_start)

    message = FakeMessage("/ocpp_start 1 10 555 0")
    await ocpp_admin.cmd_ocpp_start(message)

    assert called == []
    assert "додатним" in message.sent[0][0]


@pytest.mark.parametrize("status", [
    "unknown_station", "insufficient_balance", "not_ocpp", "not_connected", "rejected",
])
async def test_ocpp_start_reports_each_failure_status_without_crashing(monkeypatch, status):
    async def fake_start(*a, **kw):
        return ocpp_charging.ChargingStartResult(status=status, reservation_id=7, id_tag="x")
    monkeypatch.setattr(ocpp_charging, "start_charging_session", fake_start)

    message = FakeMessage(f"/ocpp_start {OPERATOR_A} {STATION_ID} {USER_ID} 20.0")
    await ocpp_admin.cmd_ocpp_start(message)

    assert len(message.sent) == 1
    assert message.sent[0][0].startswith("❌")


# ---------------------------------------------------------------------------
# /ocpp_stop
# ---------------------------------------------------------------------------

async def test_ocpp_stop_ignored_outside_admin_chat(monkeypatch):
    called = []
    async def fake_stop(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStopResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "stop_charging_session", fake_stop)

    message = FakeMessage("/ocpp_stop 1 10", chat_id=999)
    await ocpp_admin.cmd_ocpp_stop(message)

    assert message.sent == []
    assert called == []


async def test_ocpp_stop_happy_path(monkeypatch):
    calls = []
    async def fake_stop(operator_id, station_id):
        calls.append((operator_id, station_id))
        return ocpp_charging.ChargingStopResult(status="ok", transaction_id=777)
    monkeypatch.setattr(ocpp_charging, "stop_charging_session", fake_stop)

    message = FakeMessage(f"/ocpp_stop {OPERATOR_A} {STATION_ID}")
    await ocpp_admin.cmd_ocpp_stop(message)

    assert calls == [(OPERATOR_A, STATION_ID)]
    assert "777" in message.sent[0][0]


async def test_ocpp_stop_bad_args_count_gives_friendly_error_no_side_effects(monkeypatch):
    called = []
    async def fake_stop(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStopResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "stop_charging_session", fake_stop)

    message = FakeMessage("/ocpp_stop 1")
    await ocpp_admin.cmd_ocpp_stop(message)

    assert called == []
    assert "Використання" in message.sent[0][0]


async def test_ocpp_stop_no_active_session_gives_clear_answer(monkeypatch):
    async def fake_stop(*a, **kw):
        return ocpp_charging.ChargingStopResult(status="no_active_session")
    monkeypatch.setattr(ocpp_charging, "stop_charging_session", fake_stop)

    message = FakeMessage(f"/ocpp_stop {OPERATOR_A} {STATION_ID}")
    await ocpp_admin.cmd_ocpp_stop(message)

    assert "немає активної" in message.sent[0][0]


async def test_ocpp_stop_not_connected_gives_clear_answer(monkeypatch):
    async def fake_stop(*a, **kw):
        return ocpp_charging.ChargingStopResult(status="not_connected", transaction_id=777)
    monkeypatch.setattr(ocpp_charging, "stop_charging_session", fake_stop)

    message = FakeMessage(f"/ocpp_stop {OPERATOR_A} {STATION_ID}")
    await ocpp_admin.cmd_ocpp_stop(message)

    assert "не підключена" in message.sent[0][0]
    assert "777" in message.sent[0][0]


# ---------------------------------------------------------------------------
# Герметизація hold — неочікувані винятки з сервісу не мають лишати адміна
# без відповіді (feature/ocpp-hold-hardening)
# ---------------------------------------------------------------------------

async def test_ocpp_start_unexpected_exception_gives_static_answer_no_traceback_leak(monkeypatch):
    UNIQUE_MARKER = "db-connection-lost-xyz789"

    async def fake_start(*a, **kw):
        raise RuntimeError(UNIQUE_MARKER)
    monkeypatch.setattr(ocpp_charging, "start_charging_session", fake_start)

    message = FakeMessage(f"/ocpp_start {OPERATOR_A} {STATION_ID} {USER_ID} 20.0")
    await ocpp_admin.cmd_ocpp_start(message)  # не має перевикинути назовні

    assert len(message.sent) == 1
    assert message.sent[0][0].startswith("⚠️")
    assert UNIQUE_MARKER not in message.sent[0][0], "Текст винятку не має йти в чат — лише в лог"


async def test_ocpp_stop_unexpected_exception_gives_static_answer_no_traceback_leak(monkeypatch):
    UNIQUE_MARKER = "station-handshake-timeout-abc123"

    async def fake_stop(*a, **kw):
        raise TimeoutError(UNIQUE_MARKER)
    monkeypatch.setattr(ocpp_charging, "stop_charging_session", fake_stop)

    message = FakeMessage(f"/ocpp_stop {OPERATOR_A} {STATION_ID}")
    await ocpp_admin.cmd_ocpp_stop(message)  # не має перевикинути назовні

    assert len(message.sent) == 1
    assert message.sent[0][0].startswith("⚠️")
    assert UNIQUE_MARKER not in message.sent[0][0], "Текст винятку не має йти в чат — лише в лог"


# ---------------------------------------------------------------------------
# /ocpp_start_uah — Модель B (Промпт 3c-ii)
# ---------------------------------------------------------------------------

async def test_ocpp_start_uah_ignored_outside_admin_chat(monkeypatch):
    called = []
    async def fake_start(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStartUahResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "start_charging_reservation_uah", fake_start)

    message = FakeMessage("/ocpp_start_uah 1 10 100.0", chat_id=999)
    await ocpp_admin.cmd_ocpp_start_uah(message)

    assert message.sent == []
    assert called == []


async def test_ocpp_start_uah_happy_path_reports_page_url_and_ux_text(monkeypatch):
    calls = []
    async def fake_start(operator_id, station_id, hold_amount_uah, redirect_url, webhook_url, driver_contact=None):
        calls.append((operator_id, station_id, hold_amount_uah, driver_contact))
        return ocpp_charging.ChargingStartUahResult(
            status="ok", reservation_id=7, id_tag="tag123456789012",
            page_url="https://pay.monobank.ua/xyz",
        )
    monkeypatch.setattr(ocpp_charging, "start_charging_reservation_uah", fake_start)

    message = FakeMessage(f"/ocpp_start_uah {OPERATOR_A} {STATION_ID} 100.0")
    await ocpp_admin.cmd_ocpp_start_uah(message)

    assert calls == [(OPERATOR_A, STATION_ID, Decimal("100.0"), None)]
    assert len(message.sent) == 1
    text = message.sent[0][0]
    assert "#7" in text
    assert "https://pay.monobank.ua/xyz" in text
    assert "ЗАБЛОКУЄ" in text
    assert "НЕ списання" in text


async def test_ocpp_start_uah_passes_optional_driver_contact(monkeypatch):
    calls = []
    async def fake_start(operator_id, station_id, hold_amount_uah, redirect_url, webhook_url, driver_contact=None):
        calls.append(driver_contact)
        return ocpp_charging.ChargingStartUahResult(status="ok", reservation_id=1, id_tag="x", page_url="https://x")
    monkeypatch.setattr(ocpp_charging, "start_charging_reservation_uah", fake_start)

    message = FakeMessage(f"/ocpp_start_uah {OPERATOR_A} {STATION_ID} 100.0 +380501234567")
    await ocpp_admin.cmd_ocpp_start_uah(message)

    assert calls == ["+380501234567"]


async def test_ocpp_start_uah_bad_args_count_gives_friendly_error_no_side_effects(monkeypatch):
    called = []
    async def fake_start(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStartUahResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "start_charging_reservation_uah", fake_start)

    message = FakeMessage(f"/ocpp_start_uah {OPERATOR_A} {STATION_ID}")
    await ocpp_admin.cmd_ocpp_start_uah(message)

    assert called == []
    assert "Використання" in message.sent[0][0]


async def test_ocpp_start_uah_bad_number_format_gives_friendly_error_no_side_effects(monkeypatch):
    called = []
    async def fake_start(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStartUahResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "start_charging_reservation_uah", fake_start)

    message = FakeMessage(f"/ocpp_start_uah abc {STATION_ID} 100.0")
    await ocpp_admin.cmd_ocpp_start_uah(message)

    assert called == []
    assert "Биті аргументи" in message.sent[0][0]


async def test_ocpp_start_uah_non_positive_amount_rejected_no_side_effects(monkeypatch):
    called = []
    async def fake_start(*a, **kw):
        called.append((a, kw))
        return ocpp_charging.ChargingStartUahResult(status="ok")
    monkeypatch.setattr(ocpp_charging, "start_charging_reservation_uah", fake_start)

    message = FakeMessage(f"/ocpp_start_uah {OPERATOR_A} {STATION_ID} 0")
    await ocpp_admin.cmd_ocpp_start_uah(message)

    assert called == []
    assert "додатним" in message.sent[0][0]


@pytest.mark.parametrize("status", [
    "unknown_station", "not_ocpp", "no_monobank_token", "bank_error",
])
async def test_ocpp_start_uah_reports_each_failure_status_without_crashing(monkeypatch, status):
    async def fake_start(*a, **kw):
        return ocpp_charging.ChargingStartUahResult(status=status)
    monkeypatch.setattr(ocpp_charging, "start_charging_reservation_uah", fake_start)

    message = FakeMessage(f"/ocpp_start_uah {OPERATOR_A} {STATION_ID} 100.0")
    await ocpp_admin.cmd_ocpp_start_uah(message)

    assert len(message.sent) == 1
    assert message.sent[0][0].startswith("❌")


async def test_ocpp_start_uah_unexpected_exception_gives_static_answer_no_traceback_leak(monkeypatch):
    UNIQUE_MARKER = "monobank-connection-refused-xyz789"

    async def fake_start(*a, **kw):
        raise RuntimeError(UNIQUE_MARKER)
    monkeypatch.setattr(ocpp_charging, "start_charging_reservation_uah", fake_start)

    message = FakeMessage(f"/ocpp_start_uah {OPERATOR_A} {STATION_ID} 100.0")
    await ocpp_admin.cmd_ocpp_start_uah(message)  # не має перевикинути назовні

    assert len(message.sent) == 1
    assert message.sent[0][0].startswith("⚠️")
    assert UNIQUE_MARKER not in message.sent[0][0]
