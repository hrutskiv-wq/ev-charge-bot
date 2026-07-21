"""
Тести кабінету оператора в Telegram (Промпт 4): онбординг, підключення
еквайрингу, майстер станції, дії зі станціями, виручка й CSV-експорт.

Живої Postgres, Redis і мережі немає: репозиторій, bot і генератор QR
підмінені. Хендлери викликаються напряму (як у test_driver_qr.py) — фільтри
aiogram (Command/StateFilter/F.text) при цьому не задіяні, тож порядок
роутерів (app/main.py) тут не перевіряється, лише сама бізнес-логіка.

Запуск: pytest test_operator_cabinet.py -v
"""
import csv
import io
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.database import operators_repo as repo
from app.handlers import operator_billing as ob

OPERATOR_A = 1
OPERATOR_B = 2
TELEGRAM_A = 501
TELEGRAM_B = 777


# ---------------------------------------------------------------------------
# Заглушки Telegram-обʼєктів
# ---------------------------------------------------------------------------

class FakeUser:
    def __init__(self, id, username=None):
        self.id = id
        self.username = username
        self.first_name = "Тест"  # потрібен cmd_start() з app/handlers/user.py


class FakeChat:
    def __init__(self, chat_type="private", chat_id=999):
        self.type = chat_type
        self.id = chat_id


class FakeMessage:
    def __init__(self, text="", telegram_id=TELEGRAM_A, username=None,
                 chat_type="private", message_id=42):
        self.text = text
        self.from_user = FakeUser(telegram_id, username)
        self.chat = FakeChat(chat_type, chat_id=telegram_id)
        self.message_id = message_id
        self.sent = []
        self.photos = []
        self.documents = []
        self.edited = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return self

    async def answer_photo(self, photo, **kwargs):
        self.photos.append((photo, kwargs))
        return self

    async def answer_document(self, document, **kwargs):
        self.documents.append((document, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.edited.append((text, kwargs))
        return self


class FakeCallback:
    def __init__(self, data, telegram_id=TELEGRAM_A, username=None, chat_type="private"):
        self.data = data
        self.from_user = FakeUser(telegram_id, username)
        self.message = FakeMessage(telegram_id=telegram_id, chat_type=chat_type)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeFSMContext:
    def __init__(self):
        self.state = None
        self.data = {}
        self.cleared = 0

    async def clear(self):
        self.state = None
        self.data = {}
        self.cleared += 1

    async def set_state(self, state):
        self.state = state

    async def get_state(self):
        return self.state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)


class FakeBot:
    def __init__(self):
        self.deleted = []
        self.sent = []
        self.fail_delete = False

    async def delete_message(self, chat_id, message_id):
        if self.fail_delete:
            raise RuntimeError("bot has no rights to delete this message")
        self.deleted.append((chat_id, message_id))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


# ---------------------------------------------------------------------------
# Фейковий стан репозиторію
# ---------------------------------------------------------------------------

class RepoState:
    def __init__(self):
        self.operators_by_tid = {}
        self.operators_by_id = {}
        self.tokens = {}
        self.stations = {}
        self.next_operator_id = 1
        self.next_station_id = 10
        self.created_operators = []
        self.created_stations = []
        self.status_changes = []
        self.tariff_changes = []
        self.ledger_summary = {}
        self.ledger_rows = {}
        self.sessions = []

    def add_operator(self, **overrides):
        op = {"id": self.next_operator_id, "name": "Готель Едем", "phone": "+380501112233",
              "telegram_id": TELEGRAM_A, "status": "pending", "commission_pct": 4,
              "created_at": None}
        op.update(overrides)
        self.operators_by_tid[op["telegram_id"]] = op
        self.operators_by_id[op["id"]] = op
        self.next_operator_id = max(self.next_operator_id, op["id"] + 1)
        return op

    def add_station(self, **overrides):
        station = {
            "id": self.next_station_id, "operator_id": OPERATOR_A, "name": "Станція",
            "address": None, "lat": None, "lng": None, "connector_type": None,
            "power_kw": None, "mode": "manual", "ocpp_charge_point_id": None,
            "tariff_uah_kwh": 12.5, "tariff_uah_start": None, "qr_slug": "slug10",
            "status": "active", "created_at": None,
        }
        station.update(overrides)
        self.stations[station["id"]] = station
        self.next_station_id = max(self.next_station_id, station["id"] + 1)
        return station


@pytest.fixture
def rs(monkeypatch):
    st = RepoState()

    async def get_operator_by_telegram_id(telegram_id):
        return st.operators_by_tid.get(telegram_id)

    async def create_operator(name, telegram_id, phone=None, commission_pct=4):
        if telegram_id in st.operators_by_tid:
            return None
        op = st.add_operator(name=name, telegram_id=telegram_id, phone=phone, status="pending")
        st.created_operators.append({"name": name, "telegram_id": telegram_id, "phone": phone})
        return op["id"]

    async def get_operator_monobank_token_encrypted(operator_id):
        return st.tokens.get(operator_id)

    async def set_operator_monobank_token(operator_id, token_encrypted):
        st.tokens[operator_id] = token_encrypted
        return True

    async def list_stations(operator_id):
        return [s for s in st.stations.values() if s["operator_id"] == operator_id]

    async def get_station(operator_id, station_id):
        s = st.stations.get(station_id)
        if s is None or s["operator_id"] != operator_id:
            return None
        return s

    async def create_station(operator_id, name, tariff_uah_kwh, address=None, lat=None,
                             lng=None, connector_type=None, power_kw=None, mode="manual",
                             ocpp_charge_point_id=None, tariff_uah_start=None, qr_slug=None):
        station_id = st.next_station_id
        st.next_station_id += 1
        slug = qr_slug or f"slug{station_id}"
        station = {
            "id": station_id, "operator_id": operator_id, "name": name, "address": address,
            "lat": lat, "lng": lng, "connector_type": connector_type, "power_kw": power_kw,
            "mode": mode, "ocpp_charge_point_id": ocpp_charge_point_id,
            "tariff_uah_kwh": tariff_uah_kwh, "tariff_uah_start": tariff_uah_start,
            "qr_slug": slug, "status": "active", "created_at": None,
        }
        st.stations[station_id] = station
        st.created_stations.append(dict(station))
        return station_id, slug

    async def set_station_status(operator_id, station_id, status):
        s = st.stations.get(station_id)
        if s is None or s["operator_id"] != operator_id:
            return False
        s["status"] = status
        st.status_changes.append((operator_id, station_id, status))
        return True

    async def update_station_tariff(operator_id, station_id, tariff_uah_kwh, tariff_uah_start=None):
        s = st.stations.get(station_id)
        if s is None or s["operator_id"] != operator_id:
            return False
        s["tariff_uah_kwh"] = tariff_uah_kwh
        s["tariff_uah_start"] = tariff_uah_start
        st.tariff_changes.append((operator_id, station_id, tariff_uah_kwh, tariff_uah_start))
        return True

    async def get_ledger_summary(operator_id, since):
        return st.ledger_summary.get(operator_id, {})

    async def list_ledger_since(operator_id, since, limit=1000):
        return st.ledger_rows.get(operator_id, [])

    async def list_sessions(operator_id, limit=50, station_id=None):
        return [s for s in st.sessions if s["operator_id"] == operator_id][:limit]

    for name, func in [
        ("get_operator_by_telegram_id", get_operator_by_telegram_id),
        ("create_operator", create_operator),
        ("get_operator_monobank_token_encrypted", get_operator_monobank_token_encrypted),
        ("set_operator_monobank_token", set_operator_monobank_token),
        ("list_stations", list_stations),
        ("get_station", get_station),
        ("create_station", create_station),
        ("set_station_status", set_station_status),
        ("update_station_tariff", update_station_tariff),
        ("get_ledger_summary", get_ledger_summary),
        ("list_ledger_since", list_ledger_since),
        ("list_sessions", list_sessions),
    ]:
        monkeypatch.setattr(repo, name, func)

    return st


@pytest.fixture
def fake_bot(monkeypatch):
    fb = FakeBot()
    monkeypatch.setattr(ob, "bot", fb)
    return fb


@pytest.fixture
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    from app.core import crypto
    monkeypatch.setenv(crypto.ENV_VAR, Fernet.generate_key().decode())
    crypto.reset_cache()
    yield


@pytest.fixture(autouse=True)
def _no_admin_chat(monkeypatch):
    """LOGS_CHAT_ID не задано за замовчуванням — тести на нього вмикають окремо."""
    monkeypatch.delenv("LOGS_CHAT_ID", raising=False)


# ---------------------------------------------------------------------------
# Онбординг
# ---------------------------------------------------------------------------

async def test_unregistered_user_is_sent_into_onboarding(rs):
    message = FakeMessage("/operator")
    state = FakeFSMContext()

    await ob.cmd_operator_cabinet(message, state)

    assert state.state == ob.OperatorOnboarding.waiting_for_name
    assert "назву" in message.sent[0][0]


async def test_registered_operator_sees_cabinet_home_instead_of_onboarding(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A, status="active")
    message = FakeMessage("/operator")
    state = FakeFSMContext()

    await ob.cmd_operator_cabinet(message, state)

    assert state.state is None
    assert "Кабінет оператора" in message.sent[0][0]


async def test_pending_operator_sees_status_note(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A, status="pending")
    message = FakeMessage("/operator")
    state = FakeFSMContext()

    await ob.cmd_operator_cabinet(message, state)

    assert "очікує" in message.sent[0][0].lower() or "розгляд" in message.sent[0][0].lower()


async def test_onboarding_name_step_asks_for_phone_next(rs):
    state = FakeFSMContext()
    await state.set_state(ob.OperatorOnboarding.waiting_for_name)
    message = FakeMessage("Готель Едем")

    await ob.onboarding_name(message, state)

    assert state.state == ob.OperatorOnboarding.waiting_for_phone
    assert state.data["name"] == "Готель Едем"


async def test_onboarding_name_rejects_empty_string(rs):
    state = FakeFSMContext()
    await state.set_state(ob.OperatorOnboarding.waiting_for_name)
    message = FakeMessage("   ")

    await ob.onboarding_name(message, state)

    assert state.state == ob.OperatorOnboarding.waiting_for_name
    assert "порожньою" in message.sent[0][0]


async def test_onboarding_name_rejects_too_long(rs):
    state = FakeFSMContext()
    await state.set_state(ob.OperatorOnboarding.waiting_for_name)
    message = FakeMessage("Х" * 256)

    await ob.onboarding_name(message, state)

    assert state.state == ob.OperatorOnboarding.waiting_for_name


async def test_onboarding_phone_creates_pending_operator(rs, fake_bot):
    state = FakeFSMContext()
    state.data["name"] = "Готель Едем"
    await state.set_state(ob.OperatorOnboarding.waiting_for_phone)
    message = FakeMessage("+380501234567")

    await ob.onboarding_phone(message, state)

    assert rs.created_operators == [
        {"name": "Готель Едем", "telegram_id": TELEGRAM_A, "phone": "+380501234567"}
    ]
    created = rs.operators_by_tid[TELEGRAM_A]
    assert created["status"] == "pending"
    assert state.state is None
    assert "Заявку подано" in message.sent[0][0]


async def test_onboarding_phone_rejects_too_long(rs):
    state = FakeFSMContext()
    state.data["name"] = "Готель Едем"
    await state.set_state(ob.OperatorOnboarding.waiting_for_phone)
    message = FakeMessage("0" * 33)

    await ob.onboarding_phone(message, state)

    assert rs.created_operators == []
    assert state.state == ob.OperatorOnboarding.waiting_for_phone


async def test_onboarding_notifies_logs_chat_when_configured(rs, fake_bot, monkeypatch):
    monkeypatch.setenv("LOGS_CHAT_ID", "-100999")
    state = FakeFSMContext()
    state.data["name"] = "Готель Едем"
    await state.set_state(ob.OperatorOnboarding.waiting_for_phone)
    message = FakeMessage("+380501234567", username="hotel_eden")

    await ob.onboarding_phone(message, state)

    assert len(fake_bot.sent) == 1
    chat_id, text, kwargs = fake_bot.sent[0]
    assert chat_id == "-100999"
    assert "Готель Едем" in text and "+380501234567" in text and "hotel_eden" in text


async def test_onboarding_skips_notification_silently_without_logs_chat_id(rs, fake_bot):
    state = FakeFSMContext()
    state.data["name"] = "Готель Едем"
    await state.set_state(ob.OperatorOnboarding.waiting_for_phone)
    message = FakeMessage("+380501234567")

    await ob.onboarding_phone(message, state)

    assert fake_bot.sent == []


async def test_onboarding_phone_for_already_registered_telegram_id_does_not_duplicate(rs, fake_bot):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A, status="active")
    state = FakeFSMContext()
    state.data["name"] = "Друга спроба"
    await state.set_state(ob.OperatorOnboarding.waiting_for_phone)
    message = FakeMessage("+380501234567")

    await ob.onboarding_phone(message, state)

    assert "вже зареєстровані" in message.sent[0][0]
    assert fake_bot.sent == []


# ---------------------------------------------------------------------------
# Підключення еквайрингу
# ---------------------------------------------------------------------------

async def test_save_monobank_token_encrypts_deletes_and_masks(rs, fake_bot, encryption_key, monkeypatch):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    calls = []
    real_encrypt = ob.encrypt_secret

    def spy_encrypt(plaintext):
        calls.append(plaintext)
        return real_encrypt(plaintext)

    monkeypatch.setattr(ob, "encrypt_secret", spy_encrypt)

    state = FakeFSMContext()
    await state.set_state(ob.MonobankConnect.waiting_for_token)
    message = FakeMessage("merchant-secret-token-ABCD1234")

    await ob.save_monobank_token(message, state)

    assert calls == ["merchant-secret-token-ABCD1234"], "encrypt_secret має отримати саме відкритий токен"
    stored = rs.tokens[OPERATOR_A]
    assert stored != "merchant-secret-token-ABCD1234", "у сховищі має бути зашифроване значення"
    assert fake_bot.deleted == [(message.chat.id, message.message_id)]
    assert "…1234" in message.sent[0][0]
    assert "merchant-secret-token-ABCD1234" not in message.sent[0][0]
    assert state.state is None


async def test_save_monobank_token_never_appears_in_logs(rs, fake_bot, encryption_key, caplog):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    fake_bot.fail_delete = True  # найгірший випадок: гілка, що логує помилку
    state = FakeFSMContext()
    await state.set_state(ob.MonobankConnect.waiting_for_token)
    message = FakeMessage("very-secret-token-XYZ")

    with caplog.at_level("DEBUG"):
        await ob.save_monobank_token(message, state)

    assert "very-secret-token-XYZ" not in caplog.text
    assert all("very-secret-token-XYZ" not in (t or "") for t, _ in message.sent)


async def test_save_monobank_token_rejected_outside_private_chat(rs, fake_bot, encryption_key):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    state = FakeFSMContext()
    await state.set_state(ob.MonobankConnect.waiting_for_token)
    message = FakeMessage("secret-token", chat_type="group")

    await ob.save_monobank_token(message, state)

    assert OPERATOR_A not in rs.tokens
    # Аномальний шлях (кнопка мала відсіяти групу раніше), але якщо токен
    # усе ж прийшов у групу — повідомлення так само мусить бути видалене.
    assert fake_bot.deleted == [(message.chat.id, message.message_id)]
    assert "приватному чаті" in message.sent[0][0]


async def test_save_monobank_token_for_unregistered_user_does_not_crash(rs, fake_bot, encryption_key):
    state = FakeFSMContext()
    await state.set_state(ob.MonobankConnect.waiting_for_token)
    message = FakeMessage("secret-token")

    await ob.save_monobank_token(message, state)

    assert rs.tokens == {}
    assert fake_bot.deleted == [(message.chat.id, message.message_id)]
    assert "зареєструйтесь" in message.sent[0][0]


async def test_cabinet_connect_token_rejects_group_chat_before_setting_state(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    state = FakeFSMContext()
    callback = FakeCallback("opm:token", telegram_id=TELEGRAM_A, chat_type="group")

    await ob.cabinet_connect_token(callback, state)

    assert state.state is None
    assert callback.answers[0][1] is True  # show_alert


# ---------------------------------------------------------------------------
# Майстер станції
# ---------------------------------------------------------------------------

async def test_full_station_wizard_creates_manual_station_and_sends_qr(rs, monkeypatch):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    captured_urls = []

    def fake_qr(url):
        captured_urls.append(url)
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(ob, "generate_station_qr_png", fake_qr)

    state = FakeFSMContext()
    await ob.cabinet_add_station_start(FakeCallback("opm:add_station"), state)
    assert state.state == ob.StationWizard.waiting_for_name

    await ob.station_wizard_name(FakeMessage("Готель Едем — паркінг"), state)
    assert state.state == ob.StationWizard.waiting_for_address

    await ob.station_wizard_address(FakeMessage("вул. Зубра, 17"), state)
    assert state.state == ob.StationWizard.waiting_for_connector

    await ob.station_wizard_connector(FakeMessage("Type 2"), state)
    assert state.state == ob.StationWizard.waiting_for_power

    await ob.station_wizard_power(FakeMessage("22"), state)
    assert state.state == ob.StationWizard.waiting_for_tariff_kwh

    await ob.station_wizard_tariff_kwh(FakeMessage("12.50"), state)
    assert state.state == ob.StationWizard.waiting_for_tariff_start

    final_message = FakeMessage("5")
    await ob.station_wizard_tariff_start(final_message, state)

    assert state.state is None
    assert len(rs.created_stations) == 1
    created = rs.created_stations[0]
    assert created["operator_id"] == OPERATOR_A
    assert created["name"] == "Готель Едем — паркінг"
    assert created["address"] == "вул. Зубра, 17"
    assert created["connector_type"] == "Type 2"
    assert created["power_kw"] == 22.0
    assert created["tariff_uah_kwh"] == 12.5
    assert created["tariff_uah_start"] == 5.0
    assert created["mode"] == "manual"

    assert len(final_message.photos) == 1
    assert captured_urls == [f"{ob.PUBLIC_BASE_URL}/s/{created['qr_slug']}"]


async def test_station_wizard_skips_optional_fields_with_dash(rs, monkeypatch):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    monkeypatch.setattr(ob, "generate_station_qr_png", lambda url: b"\x89PNG\r\n\x1a\n")

    state = FakeFSMContext()
    await state.set_state(ob.StationWizard.waiting_for_name)
    await ob.station_wizard_name(FakeMessage("Мінімальна станція"), state)
    await ob.station_wizard_address(FakeMessage("-"), state)
    await ob.station_wizard_connector(FakeMessage("-"), state)
    await ob.station_wizard_power(FakeMessage("-"), state)
    await ob.station_wizard_tariff_kwh(FakeMessage("10"), state)
    await ob.station_wizard_tariff_start(FakeMessage("-"), state)

    created = rs.created_stations[0]
    assert created["address"] is None
    assert created["connector_type"] is None
    assert created["power_kw"] is None
    assert created["tariff_uah_start"] is None


async def test_station_wizard_rejects_non_numeric_tariff_and_stays_on_step(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    state = FakeFSMContext()
    await state.set_state(ob.StationWizard.waiting_for_tariff_kwh)
    message = FakeMessage("не число")

    await ob.station_wizard_tariff_kwh(message, state)

    assert state.state == ob.StationWizard.waiting_for_tariff_kwh
    assert "додатним числом" in message.sent[0][0]


async def test_station_wizard_rejects_negative_power(rs):
    state = FakeFSMContext()
    await state.set_state(ob.StationWizard.waiting_for_power)
    message = FakeMessage("-5")  # мінус, а не "-" (пропуск)

    await ob.station_wizard_power(message, state)

    assert state.state == ob.StationWizard.waiting_for_power


async def test_station_wizard_name_rejects_empty_and_bare_dash(rs):
    state = FakeFSMContext()
    await state.set_state(ob.StationWizard.waiting_for_name)

    await ob.station_wizard_name(FakeMessage("-"), state)
    assert state.state == ob.StationWizard.waiting_for_name

    await ob.station_wizard_name(FakeMessage("   "), state)
    assert state.state == ob.StationWizard.waiting_for_name


# ---------------------------------------------------------------------------
# Дії зі станціями: перегляд, тариф, увімкнути/вимкнути, QR
# ---------------------------------------------------------------------------

async def test_station_action_view_shows_detail(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_station(id=10, operator_id=OPERATOR_A, name="Станція А")
    callback = FakeCallback("opst:10:view")

    await ob.station_action(callback, FakeFSMContext())

    assert "Станція А" in callback.message.edited[0][0]


async def test_station_action_toggle_flips_status(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_station(id=10, operator_id=OPERATOR_A, status="active")
    callback = FakeCallback("opst:10:toggle")

    await ob.station_action(callback, FakeFSMContext())

    assert rs.stations[10]["status"] == "disabled"
    assert rs.status_changes == [(OPERATOR_A, 10, "disabled")]


async def test_station_action_qr_resends_photo_with_correct_url(rs, monkeypatch):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_station(id=10, operator_id=OPERATOR_A, qr_slug="qr-abc")
    captured = []
    monkeypatch.setattr(ob, "generate_station_qr_png", lambda url: captured.append(url) or b"\x89PNG")
    callback = FakeCallback("opst:10:qr")

    await ob.station_action(callback, FakeFSMContext())

    assert captured == [f"{ob.PUBLIC_BASE_URL}/s/qr-abc"]
    assert len(callback.message.photos) == 1


async def test_station_action_tariff_enters_edit_state_with_station_id(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_station(id=10, operator_id=OPERATOR_A)
    state = FakeFSMContext()
    callback = FakeCallback("opst:10:tariff")

    await ob.station_action(callback, state)

    assert state.state == ob.TariffEdit.waiting_for_new_tariff
    assert state.data["station_id"] == 10


async def test_tariff_edit_preserves_existing_start_fee(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_station(id=10, operator_id=OPERATOR_A, tariff_uah_kwh=12.5, tariff_uah_start=5.0)
    state = FakeFSMContext()
    state.data["station_id"] = 10
    await state.set_state(ob.TariffEdit.waiting_for_new_tariff)

    await ob.tariff_edit_apply(FakeMessage("15.00"), state)

    assert rs.tariff_changes == [(OPERATOR_A, 10, 15.0, 5.0)]
    assert state.state is None


async def test_tariff_edit_rejects_invalid_value(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_station(id=10, operator_id=OPERATOR_A)
    state = FakeFSMContext()
    state.data["station_id"] = 10
    await state.set_state(ob.TariffEdit.waiting_for_new_tariff)

    await ob.tariff_edit_apply(FakeMessage("0"), state)

    assert rs.tariff_changes == []
    assert state.state == ob.TariffEdit.waiting_for_new_tariff


# ---------------------------------------------------------------------------
# Ізоляція тенантів на рівні хендлерів
# ---------------------------------------------------------------------------

async def test_operator_b_cannot_view_operator_a_station(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_operator(id=OPERATOR_B, telegram_id=TELEGRAM_B)
    rs.add_station(id=10, operator_id=OPERATOR_A, name="Станція А")
    callback = FakeCallback("opst:10:view", telegram_id=TELEGRAM_B)

    await ob.station_action(callback, FakeFSMContext())

    assert callback.answers[0][1] is True  # show_alert "Станцію не знайдено"
    assert callback.message.edited == []


async def test_operator_b_cannot_toggle_operator_a_station(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_operator(id=OPERATOR_B, telegram_id=TELEGRAM_B)
    rs.add_station(id=10, operator_id=OPERATOR_A, status="active")
    callback = FakeCallback("opst:10:toggle", telegram_id=TELEGRAM_B)

    await ob.station_action(callback, FakeFSMContext())

    assert rs.stations[10]["status"] == "active", "Статус чужої станції не мав змінитись"
    assert rs.status_changes == []


async def test_operator_b_cannot_edit_tariff_of_operator_a_station(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_operator(id=OPERATOR_B, telegram_id=TELEGRAM_B)
    rs.add_station(id=10, operator_id=OPERATOR_A, tariff_uah_kwh=12.5)
    state = FakeFSMContext()
    state.data["station_id"] = 10
    await state.set_state(ob.TariffEdit.waiting_for_new_tariff)
    message = FakeMessage("999", telegram_id=TELEGRAM_B)

    await ob.tariff_edit_apply(message, state)

    assert rs.tariff_changes == []
    assert rs.stations[10]["tariff_uah_kwh"] == 12.5


async def test_start_command_clears_station_wizard_state(rs, monkeypatch):
    """
    Регресія на вибір порядку роутерів (app/main.py): /start посеред майстра
    станції має вивести користувача з FSM. Якби cmd_start не чистив стан,
    наступне вільне повідомлення користувача (напр. звернення до ШІ-чату)
    після /start і надалі ловилось би хендлером цього кроку майстра як
    "адреса станції", а не дійшло б до свого справжнього хендлера.
    """
    from app.handlers import user as user_handlers

    async def fake_get_user_data(user_id):
        return 0.0, 0.0

    monkeypatch.setattr(user_handlers, "get_user_data", fake_get_user_data)

    state = FakeFSMContext()
    await state.set_state(ob.StationWizard.waiting_for_name)
    message = FakeMessage("/start")

    await user_handlers.cmd_start(message, state)

    assert state.state is None


async def test_operator_b_stations_list_excludes_operator_a(rs):
    """
    Список станцій показує назви в кнопках reply_markup, а не в тексті
    повідомлення — тому перевіряти треба саме клавіатуру, інакше assert
    "Станція Б" not in text проходить завжди незалежно від того, що
    насправді потрапило в список.
    """
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_operator(id=OPERATOR_B, telegram_id=TELEGRAM_B)
    rs.add_station(id=10, operator_id=OPERATOR_A, name="Станція А")
    rs.add_station(id=11, operator_id=OPERATOR_B, name="Станція Б")
    callback = FakeCallback("opm:stations", telegram_id=TELEGRAM_B)

    await ob.cabinet_station_list(callback)

    _text, kwargs = callback.message.edited[0]
    button_texts = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]

    assert any("Станція Б" in t for t in button_texts)
    assert not any("Станція А" in t for t in button_texts)


# ---------------------------------------------------------------------------
# Виручка: чисті функції
# ---------------------------------------------------------------------------

NOW = datetime(2026, 7, 21, 15, 30, tzinfo=timezone.utc)


def test_period_since_today_is_midnight_utc():
    since = ob._period_since("today", now=NOW)
    assert since == datetime(2026, 7, 21, 0, 0, 0, tzinfo=timezone.utc)


def test_period_since_week_is_seven_days_back():
    since = ob._period_since("week", now=NOW)
    assert (NOW - since).days == 7


def test_period_since_month_is_thirty_days_back():
    since = ob._period_since("month", now=NOW)
    assert (NOW - since).days == 30


def test_period_since_rejects_unknown_period():
    with pytest.raises(ValueError):
        ob._period_since("year", now=NOW)


def test_summarize_ledger_computes_gross_commission_net():
    gross, commission, net = ob._summarize_ledger(
        {"session_income": Decimal("300.00"), "platform_commission": Decimal("-12.00")}
    )
    assert (gross, commission, net) == (Decimal("300.00"), Decimal("-12.00"), Decimal("288.00"))


def test_summarize_ledger_defaults_missing_types_to_zero():
    gross, commission, net = ob._summarize_ledger({})
    assert (gross, commission, net) == (Decimal("0"), Decimal("0"), Decimal("0"))


# ---------------------------------------------------------------------------
# Виручка: хендлери
# ---------------------------------------------------------------------------

async def test_revenue_period_handler_shows_correct_sums_and_sessions(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.ledger_summary[OPERATOR_A] = {
        "session_income": Decimal("300.00"), "platform_commission": Decimal("-12.00"),
    }
    rs.sessions = [
        {"id": 1, "operator_id": OPERATOR_A, "status": "completed", "amount_uah": Decimal("300.00")},
    ]
    callback = FakeCallback("oprev:today")

    await ob.cabinet_revenue_period(callback)

    text = callback.message.edited[0][0]
    assert "300.00" in text and "-12.00" in text and "288.00" in text
    assert "#1" in text


async def test_revenue_period_rejects_unknown_period(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    callback = FakeCallback("oprev:decade")

    await ob.cabinet_revenue_period(callback)

    assert callback.answers[0][1] is True
    assert callback.message.edited == []


async def test_revenue_period_isolates_operator_b_from_operator_a_ledger(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.add_operator(id=OPERATOR_B, telegram_id=TELEGRAM_B)
    rs.ledger_summary[OPERATOR_A] = {"session_income": Decimal("300.00")}
    # Оператор Б не має записів -> summary порожній -> нулі.
    callback = FakeCallback("oprev:today", telegram_id=TELEGRAM_B)

    await ob.cabinet_revenue_period(callback)

    text = callback.message.edited[0][0]
    assert "0.00" in text
    assert "300.00" not in text


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def test_build_ledger_csv_has_correct_header_and_rows():
    rows = [
        {"id": 1, "session_id": 77, "type": "session_income", "amount_uah": Decimal("300.00"),
         "description": "Оплата сесії #77", "created_at": datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc)},
        {"id": 2, "session_id": 77, "type": "platform_commission", "amount_uah": Decimal("-12.00"),
         "description": "Комісія платформи 4%", "created_at": datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc)},
        {"id": 3, "session_id": None, "type": "payout", "amount_uah": Decimal("-500.00"),
         "description": None, "created_at": datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)},
    ]

    csv_bytes = ob._build_ledger_csv(rows)
    text = csv_bytes.decode("utf-8-sig")
    parsed = list(csv.reader(io.StringIO(text)))

    assert parsed[0] == ob._CSV_HEADER
    assert parsed[1] == ["1", "2026-07-21 14:30", "session_income", "300.00", "77", "Оплата сесії #77"]
    assert parsed[2] == ["2", "2026-07-21 14:30", "platform_commission", "-12.00", "77", "Комісія платформи 4%"]
    assert parsed[3] == ["3", "2026-07-22 09:00", "payout", "-500.00", "", ""]

    amounts = [Decimal(row[3]) for row in parsed[1:]]
    assert sum(amounts) == Decimal("-212.00")


async def test_revenue_csv_handler_sends_document_for_the_selected_period(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    rs.ledger_rows[OPERATOR_A] = [
        {"id": 1, "session_id": 5, "type": "session_income", "amount_uah": Decimal("100.00"),
         "description": None, "created_at": NOW},
    ]
    callback = FakeCallback("opcsv:week")

    await ob.cabinet_revenue_csv(callback)

    assert len(callback.message.documents) == 1
    _document, kwargs = callback.message.documents[0]
    assert "week" in kwargs.get("caption", "") or "тиждень" in kwargs.get("caption", "")


async def test_revenue_csv_handler_rejects_unknown_period(rs):
    rs.add_operator(id=OPERATOR_A, telegram_id=TELEGRAM_A)
    callback = FakeCallback("opcsv:decade")

    await ob.cabinet_revenue_csv(callback)

    assert callback.answers[0][1] is True
    assert callback.message.documents == []


# ---------------------------------------------------------------------------
# QR PNG — реальна бібліотека, без моків
# ---------------------------------------------------------------------------

def test_generate_station_qr_png_produces_a_valid_png():
    from app.services.qr_image import generate_station_qr_png

    png_bytes = generate_station_qr_png("https://evolt.ua/s/abc123")

    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n"), "Немає PNG-сигнатури на початку файлу"
    assert len(png_bytes) > 100


# ---------------------------------------------------------------------------
# Допоміжні чисті функції
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [("hello", True), ("/start", False), ("/operator arg", False)])
def test_is_free_text_excludes_commands(text, expected):
    assert ob._is_free_text(FakeMessage(text)) is expected


@pytest.mark.parametrize("raw,expected", [("-", None), (" - ", None), ("вул. Франка", "вул. Франка")])
def test_parse_skip(raw, expected):
    assert ob._parse_skip(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("12.5", Decimal("12.5")), ("12,5", Decimal("12.5")), ("0", None), ("-5", None), ("abc", None),
])
def test_parse_positive_decimal(raw, expected):
    assert ob._parse_positive_decimal(raw) == expected
