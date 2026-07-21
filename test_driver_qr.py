"""
Тести Промпту 2b: сторінка оплати водія /s/{qr_slug}, чек і підтвердження
оператора.

Водій не автентифікований — єдиний ключ доступу це qr_slug. Тому окремо
перевіряється, що знання одного slug не дає читати чужі сесії, а знання
формату callback_data не дає керувати чужими станціями.

Живої Postgres і мережі немає: репозиторій та банк підмінені.

Запуск: pytest test_driver_qr.py -v
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import driver_qr
from app.database import operators_repo as repo
from app.handlers import operator_billing
from app.services import monobank_acquiring, operator_notify

OPERATOR_A = 1
OPERATOR_B = 2
SLUG = "abc123slug"
STARTED = datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc)


def make_station(**overrides):
    station = {
        "id": 10, "operator_id": OPERATOR_A, "name": "Готель Едем",
        "address": "вул. Зубра, 17", "lat": None, "lng": None,
        "connector_type": "Type 2", "power_kw": Decimal("22.00"),
        "mode": "manual", "ocpp_charge_point_id": None,
        "tariff_uah_kwh": Decimal("18.00"), "tariff_uah_start": None,
        "qr_slug": SLUG, "status": "active", "created_at": STARTED,
    }
    station.update(overrides)
    return station


def make_session(**overrides):
    session = {
        "id": 77, "operator_id": OPERATOR_A, "station_id": 10,
        "started_at": STARTED, "ended_at": None, "kwh": None,
        "amount_uah": Decimal("200.00"), "payment_id": 5,
        "status": "paid", "driver_contact": None, "created_at": STARTED,
    }
    session.update(overrides)
    return session


class FakeState:
    def __init__(self):
        self.stations = {SLUG: make_station()}
        self.sessions = {}
        self.payments = []
        self.tokens = {OPERATOR_A: None}
        self.attached = []
        self.next_session_id = 77
        self.invoice_error = None
        self.created_invoices = []
        self.operators = {OPERATOR_A: {"id": OPERATOR_A, "telegram_id": 501,
                                       "status": "active", "commission_pct": 4}}


@pytest.fixture
def state(monkeypatch, encryption_key):
    from app.core import crypto

    st = FakeState()
    st.tokens[OPERATOR_A] = crypto.encrypt_secret("merchant-token-A")

    async def get_station_by_qr_slug(qr_slug):
        return st.stations.get(qr_slug)

    async def get_operator_monobank_token_encrypted(operator_id):
        return st.tokens.get(operator_id)

    async def get_operator(operator_id):
        return st.operators.get(operator_id)

    async def create_session(operator_id, station_id, amount_uah=None,
                             payment_id=None, driver_contact=None):
        session_id = st.next_session_id
        st.next_session_id += 1
        st.sessions[session_id] = make_session(
            id=session_id, operator_id=operator_id, station_id=station_id,
            amount_uah=amount_uah, status="pending", payment_id=None,
        )
        return session_id

    async def create_operator_payment(operator_id, invoice_id, amount_uah, status="pending"):
        st.payments.append({"operator_id": operator_id, "invoice_id": invoice_id,
                            "amount_uah": amount_uah})
        return len(st.payments)

    async def attach_payment_to_session(operator_id, session_id, payment_id):
        st.attached.append((operator_id, session_id, payment_id))
        return True

    async def set_session_status(operator_id, session_id, status, payment_id=None):
        session = st.sessions.get(session_id)
        if session is None or session["operator_id"] != operator_id:
            return False
        session["status"] = status
        return True

    async def get_session(operator_id, session_id):
        session = st.sessions.get(session_id)
        if session is None or session["operator_id"] != operator_id:
            return None
        return session

    async def create_invoice(token, amount_uah, reference, redirect_url,
                             webhook_url, destination=None):
        if st.invoice_error:
            raise st.invoice_error
        st.created_invoices.append({
            "token": token, "amount_uah": amount_uah, "reference": reference,
            "redirect_url": redirect_url, "webhook_url": webhook_url,
            "destination": destination,
        })
        return {"invoiceId": "inv-xyz", "pageUrl": "https://pay.mbnk.test/inv-xyz"}

    for name, func in [
        ("get_station_by_qr_slug", get_station_by_qr_slug),
        ("get_operator_monobank_token_encrypted", get_operator_monobank_token_encrypted),
        ("get_operator", get_operator),
        ("create_session", create_session),
        ("create_operator_payment", create_operator_payment),
        ("attach_payment_to_session", attach_payment_to_session),
        ("set_session_status", set_session_status),
        ("get_session", get_session),
    ]:
        monkeypatch.setattr(repo, name, func)
    monkeypatch.setattr(driver_qr, "create_invoice", create_invoice)
    return st


@pytest.fixture
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    from app.core import crypto
    monkeypatch.setenv(crypto.ENV_VAR, Fernet.generate_key().decode())
    crypto.reset_cache()
    yield
    crypto.reset_cache()


@pytest.fixture
def client(state):
    app = FastAPI()
    app.include_router(driver_qr.driver_router)
    # follow_redirects=False — редірект у банк це і є результат, який перевіряємо
    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Сторінка станції
# ---------------------------------------------------------------------------

def test_station_page_shows_tariff_and_payment_form(client):
    response = client.get(f"/s/{SLUG}")

    assert response.status_code == 200
    body = response.text
    assert "Готель Едем" in body
    assert "вул. Зубра, 17" in body
    assert "18 грн / кВт·год" in body          # тариф без зайвих нулів
    assert f'action="/s/{SLUG}/start"' in body
    assert "Вільна" in body
    for preset in driver_qr.AMOUNT_PRESETS:
        assert f"{preset} ₴" in body


def test_station_page_is_self_contained(client):
    """
    Водій біля станції часто на слабкому мобільному інтернеті або в
    підземному паркінгу. Жодних зовнішніх скриптів, шрифтів і CDN —
    сторінка має відкриватись одним запитом.
    """
    body = client.get(f"/s/{SLUG}").text
    assert "<script" not in body.lower()
    assert "http://" not in body.replace("http-equiv", "")
    assert "cdn" not in body.lower()


def test_unknown_slug_returns_404_page(client):
    response = client.get("/s/такого-немає")
    assert response.status_code == 404
    assert "Станцію не знайдено" in response.text


@pytest.mark.parametrize("status,label", [
    ("offline", "Немає звʼязку"),
    ("disabled", "Вимкнена"),
])
def test_unavailable_station_shows_no_payment_form(status, label, state, client):
    state.stations[SLUG] = make_station(status=status)

    response = client.get(f"/s/{SLUG}")

    assert response.status_code == 200
    assert label in response.text
    assert f'action="/s/{SLUG}/start"' not in response.text


def test_start_fee_shown_when_configured(state, client):
    state.stations[SLUG] = make_station(tariff_uah_start=Decimal("15.00"))
    assert "Плата за старт" in client.get(f"/s/{SLUG}").text


# ---------------------------------------------------------------------------
# Створення інвойсу
# ---------------------------------------------------------------------------

def test_start_creates_session_invoice_and_redirects_to_bank(state, client):
    response = client.post(f"/s/{SLUG}/start", data={"amount_uah": "200"})

    assert response.status_code == 303
    assert response.headers["location"] == "https://pay.mbnk.test/inv-xyz"

    assert len(state.created_invoices) == 1
    invoice = state.created_invoices[0]
    assert invoice["amount_uah"] == Decimal("200.00")
    assert invoice["token"] == "merchant-token-A"       # розшифрований токен оператора
    assert invoice["reference"] == "session-77"
    assert invoice["webhook_url"].endswith(f"/webhook/operator/{OPERATOR_A}")
    assert invoice["redirect_url"].endswith(f"/s/{SLUG}/receipt/77")

    assert state.payments == [{"operator_id": OPERATOR_A, "invoice_id": "inv-xyz",
                               "amount_uah": Decimal("200.00")}]
    assert state.attached == [(OPERATOR_A, 77, 1)]


def test_custom_amount_wins_over_preset(state, client):
    """
    Водій вписав свою суму, але радіокнопка лишилась вибраною — платити має
    саме вписане, інакше з картки спишеться не те, що людина ввела.
    """
    client.post(f"/s/{SLUG}/start",
                data={"amount_uah": "200", "custom_amount": "137.50"})

    assert state.created_invoices[0]["amount_uah"] == Decimal("137.50")


def test_comma_decimal_separator_accepted(state, client):
    """На українській розкладці кома — звичний десятковий роздільник."""
    client.post(f"/s/{SLUG}/start", data={"custom_amount": "137,50"})
    assert state.created_invoices[0]["amount_uah"] == Decimal("137.50")


@pytest.mark.parametrize("amount,expected_message", [
    ("0", "додатним"),
    ("-50", "додатним"),
    ("5", "Мінімальна сума"),
    ("999999", "Максимальна сума"),
    ("багато", "числом"),
    ("", "Вкажіть суму"),
])
def test_invalid_amounts_are_rejected_without_creating_anything(
        amount, expected_message, state, client):
    response = client.post(f"/s/{SLUG}/start", data={"custom_amount": amount})

    assert response.status_code == 400
    assert expected_message in response.text
    assert state.created_invoices == []
    assert state.sessions == {}


def test_payment_on_inactive_station_is_refused(state, client):
    state.stations[SLUG] = make_station(status="disabled")

    response = client.post(f"/s/{SLUG}/start", data={"amount_uah": "200"})

    assert response.status_code == 400
    assert state.created_invoices == []
    assert state.sessions == {}


def test_operator_without_acquiring_token_gets_neutral_error(state, client):
    """Провина оператора, не водія — текст не має звинувачувати водія."""
    state.tokens[OPERATOR_A] = None

    response = client.post(f"/s/{SLUG}/start", data={"amount_uah": "200"})

    assert response.status_code == 400
    assert "Оператор ще не завершив налаштування" in response.text
    assert state.sessions == {}


def test_bank_failure_marks_session_failed_and_shows_retry(state, client):
    """
    Сесія створюється ДО інвойсу, тож якщо банк не відповів — вона не має
    лишитись висіти в 'pending' і псувати звірку.
    """
    state.invoice_error = monobank_acquiring.MonobankError("банк лежить")

    response = client.post(f"/s/{SLUG}/start", data={"amount_uah": "200"})

    assert response.status_code == 400
    assert "Банк тимчасово недоступний" in response.text
    assert state.sessions[77]["status"] == "failed"
    assert state.payments == []


# ---------------------------------------------------------------------------
# Чек
# ---------------------------------------------------------------------------

def test_receipt_shows_amount_and_state(state, client):
    state.sessions[77] = make_session(status="paid")

    response = client.get(f"/s/{SLUG}/receipt/77")

    assert response.status_code == 200
    assert "200 ₴" in response.text
    assert "Оплату отримано" in response.text
    assert "#77" in response.text


@pytest.mark.parametrize("status,expected", [
    ("pending", "ще не підтверджена"),
    ("charging", "зарядка триває"),
    ("completed", "Сесію завершено"),
    ("failed", "не пройшла"),
])
def test_receipt_reflects_session_status(status, expected, state, client):
    state.sessions[77] = make_session(status=status)
    assert expected in client.get(f"/s/{SLUG}/receipt/77").text


def test_receipt_of_session_from_another_station_is_not_readable(state, client):
    """
    Сесія того самого оператора, але іншої станції. Знаючи один slug, водій
    не має перебором session_id читати чужі сесії.
    """
    state.sessions[99] = make_session(id=99, station_id=42)

    response = client.get(f"/s/{SLUG}/receipt/99")

    assert response.status_code == 404
    assert "200 ₴" not in response.text


def test_receipt_of_unknown_session_is_indistinguishable_from_foreign_one(state, client):
    state.sessions[99] = make_session(id=99, station_id=42)

    foreign = client.get(f"/s/{SLUG}/receipt/99")
    missing = client.get(f"/s/{SLUG}/receipt/12345")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.text == missing.text


def test_pending_receipt_auto_refreshes(state, client):
    state.sessions[77] = make_session(status="pending")
    assert "http-equiv=\"refresh\"" in client.get(f"/s/{SLUG}/receipt/77").text


def test_completed_receipt_does_not_auto_refresh(state, client):
    state.sessions[77] = make_session(status="completed", kwh=Decimal("11.500"))
    body = client.get(f"/s/{SLUG}/receipt/77").text
    assert "http-equiv=\"refresh\"" not in body
    assert "11.5 кВт·год" in body


# ---------------------------------------------------------------------------
# Пуш оператору
# ---------------------------------------------------------------------------

def test_paid_message_contains_what_operator_needs_to_act():
    text = operator_notify.build_paid_message("Готель Едем", Decimal("200.00"), 77)

    assert "200.00 грн" in text
    assert "Готель Едем" in text
    assert "#77" in text
    assert "Увімкніть станцію" in text


def test_confirm_button_carries_operator_and_session():
    keyboard = operator_notify.build_confirm_keyboard(OPERATOR_A, 77)
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data == f"{operator_notify.CONFIRM_PREFIX}:{OPERATOR_A}:77"


async def test_notification_failure_never_breaks_payment_flow(monkeypatch):
    """
    Гроші вже прийшли. Заблокований бот чи лежачий Telegram не привід
    вертати банку помилку — функція має проковтнути виняток і повернути False.
    """
    class BrokenBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("bot was blocked by the user")

    import app.core.loader as loader
    monkeypatch.setattr(loader, "bot", BrokenBot(), raising=False)

    result = await operator_notify.notify_operator_paid(
        telegram_id=555, operator_id=OPERATOR_A, session_id=77,
        station_name="Готель Едем", amount_uah=Decimal("200.00"),
    )
    assert result is False


# ---------------------------------------------------------------------------
# Підтвердження «увімкнув станцію»
# ---------------------------------------------------------------------------

class FakeCallback:
    def __init__(self, data, telegram_id):
        self.data = data
        self.from_user = type("U", (), {"id": telegram_id})()
        self.answers = []
        self.message = self

        # для message.edit_text
        self.html_text = "💳 Оплачено"
        self.edited = None

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_text(self, text, parse_mode=None):
        self.edited = text


@pytest.fixture
def confirm_state(monkeypatch):
    st = FakeState()
    st.sessions[77] = make_session(status="paid")
    st.operators = {555: {"id": OPERATOR_A, "telegram_id": 555, "commission_pct": 4}}

    async def get_operator_by_telegram_id(telegram_id):
        return st.operators.get(telegram_id)

    async def get_session(operator_id, session_id):
        session = st.sessions.get(session_id)
        if session is None or session["operator_id"] != operator_id:
            return None
        return session

    async def set_session_status(operator_id, session_id, status, payment_id=None):
        st.sessions[session_id]["status"] = status
        return True

    monkeypatch.setattr(repo, "get_operator_by_telegram_id", get_operator_by_telegram_id)
    monkeypatch.setattr(repo, "get_session", get_session)
    monkeypatch.setattr(repo, "set_session_status", set_session_status)
    return st


async def test_operator_confirmation_moves_session_to_charging(confirm_state):
    callback = FakeCallback(f"{operator_notify.CONFIRM_PREFIX}:{OPERATOR_A}:77", 555)

    await operator_billing.confirm_station_switched_on(callback)

    assert confirm_state.sessions[77]["status"] == "charging"
    assert "Станцію увімкнено" in callback.edited


async def test_stranger_cannot_confirm_someone_elses_session(confirm_state):
    """
    Формат callback_data не є секретом. Підтвердити сесію може лише той
    Telegram-акаунт, що справді є цим оператором.
    """
    callback = FakeCallback(f"{operator_notify.CONFIRM_PREFIX}:{OPERATOR_A}:77", 999)

    await operator_billing.confirm_station_switched_on(callback)

    assert confirm_state.sessions[77]["status"] == "paid", "Чужа сесія змінена"
    assert callback.answers[0][1] is True  # show_alert
    assert callback.edited is None


async def test_operator_cannot_confirm_session_of_another_operator(confirm_state):
    """Оператор Б підставляє свій id у callback — сесія А не його."""
    confirm_state.operators[555] = {"id": OPERATOR_B, "telegram_id": 555,
                                    "commission_pct": 4}
    callback = FakeCallback(f"{operator_notify.CONFIRM_PREFIX}:{OPERATOR_A}:77", 555)

    await operator_billing.confirm_station_switched_on(callback)

    assert confirm_state.sessions[77]["status"] == "paid"


async def test_double_confirmation_is_harmless(confirm_state):
    callback = FakeCallback(f"{operator_notify.CONFIRM_PREFIX}:{OPERATOR_A}:77", 555)
    await operator_billing.confirm_station_switched_on(callback)

    second = FakeCallback(f"{operator_notify.CONFIRM_PREFIX}:{OPERATOR_A}:77", 555)
    await operator_billing.confirm_station_switched_on(second)

    assert confirm_state.sessions[77]["status"] == "charging"
    assert second.answers == [("Уже підтверджено", False)]


async def test_unpaid_session_cannot_be_confirmed(confirm_state):
    confirm_state.sessions[77]["status"] = "pending"
    callback = FakeCallback(f"{operator_notify.CONFIRM_PREFIX}:{OPERATOR_A}:77", 555)

    await operator_billing.confirm_station_switched_on(callback)

    assert confirm_state.sessions[77]["status"] == "pending"


@pytest.mark.parametrize("data", ["opsess:on:зламано", "opsess:on", "", "opsess:on:1:x"])
async def test_malformed_callback_data_is_rejected(data, confirm_state):
    callback = FakeCallback(data, 555)
    await operator_billing.confirm_station_switched_on(callback)
    assert confirm_state.sessions[77]["status"] == "paid"


@pytest.mark.parametrize("operator_status", ["suspended", "pending"])
def test_inactive_operator_cannot_accept_payments(operator_status, state, client):
    """
    Оператор на паузі (несплачена підписка) або з незавершеним онбордингом
    не має приймати гроші водіїв — інакше ми зберемо оплати, за які ніхто
    не відповідає. Станція при цьому може бути цілком 'active'.
    """
    state.operators[OPERATOR_A]["status"] = operator_status

    response = client.post(f"/s/{SLUG}/start", data={"amount_uah": "200"})

    assert response.status_code == 400
    assert "Станція зараз недоступна для оплати" in response.text
    assert state.created_invoices == [], "Банк викликали для неактивного оператора"
    assert state.sessions == {}, "Сесію створено для неактивного оператора"


def test_missing_operator_record_blocks_payment(state, client):
    """Станція є, а оператора немає — теж не приймаємо оплату."""
    state.operators.clear()

    response = client.post(f"/s/{SLUG}/start", data={"amount_uah": "200"})

    assert response.status_code == 400
    assert state.created_invoices == []
    assert state.sessions == {}


def test_operator_status_is_checked_before_touching_the_bank(state, client):
    """
    Перевірка статусу оператора має стояти ДО валідації суми й створення
    сесії: інакше на кожну спробу оплати неактивному оператору ми б плодили
    сміттєві 'pending'-сесії.
    """
    state.operators[OPERATOR_A]["status"] = "suspended"

    client.post(f"/s/{SLUG}/start", data={"custom_amount": "не-число"})

    assert state.sessions == {}
    assert state.created_invoices == []
