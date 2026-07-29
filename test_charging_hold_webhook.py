"""
Тести на app/api/charging_hold_webhook.py — вебхук підтвердження hold-
інвойсу Моделі B (Промпт 3c-ii).

Головне, що тут перевіряється (той самий принцип, що test_operator_
payments.py): тіло webhook НЕ є джерелом правди — лише СВІЖА відповідь
банку (get_invoice_status). Плюс герметизація RemoteStart (правка 5
рев'ю): будь-яка невдача між мʼютексним переходом і підтвердженим
RemoteStart компенсується НЕГАЙНИМ cancel_invoice(), а не release_
reservation_hold() чи очікуванням звірки.

Живих мережевих викликів і живої Postgres немає — репозиторій і клієнт
банку підмінені фейками (як у test_operator_payments.py).

Запуск: pytest test_charging_hold_webhook.py -v
"""
import json

import pytest

from app.api import charging_hold_webhook as webhook
from app.api.ocpp_ws import ChargePointNotConnected
from app.database import operators_repo as repo

OPERATOR_A = 1
OPERATOR_B = 2
INVOICE = "inv-hold-abc123"
RESERVATION_ID = 42


class FakeChargingHoldState:
    def __init__(self):
        self.reservations = {}  # (operator_id, invoice_id) -> dict
        self.tokens = {}         # operator_id -> "encrypted"
        self.stations = {}       # (operator_id, station_id) -> dict
        self.status_calls = 0
        self.remote_start_calls = []
        self.cancel_calls = []
        self.confirm_calls = []

    def add_reservation(self, operator_id, invoice_id, reservation_id, status="awaiting_hold",
                        station_id=10, id_tag="tag-1"):
        self.reservations[(operator_id, invoice_id)] = {
            "id": reservation_id, "operator_id": operator_id, "invoice_id": invoice_id,
            "status": status, "station_id": station_id, "id_tag": id_tag,
            "final_amount_uah": None,
        }

    def add_station(self, operator_id, station_id, cp_id="CP-1"):
        self.stations[(operator_id, station_id)] = {
            "id": station_id, "operator_id": operator_id, "ocpp_charge_point_id": cp_id,
        }

    def get(self, reservation_id):
        for r in self.reservations.values():
            if r["id"] == reservation_id:
                return r
        return None


@pytest.fixture
def billing(monkeypatch):
    state = FakeChargingHoldState()

    async def get_reservation_by_invoice_id(operator_id, invoice_id):
        r = state.reservations.get((operator_id, invoice_id))
        return dict(r) if r is not None else None

    async def get_operator_monobank_token_encrypted(operator_id):
        return state.tokens.get(operator_id)

    async def mark_reservation_hold_confirmed(operator_id, reservation_id):
        r = state.get(reservation_id)
        if r is None or r["operator_id"] != operator_id or r["status"] != "awaiting_hold":
            return False
        r["status"] = "pending"
        state.confirm_calls.append(reservation_id)
        return True

    async def get_station(operator_id, station_id):
        s = state.stations.get((operator_id, station_id))
        return dict(s) if s is not None else None

    async def record_uah_settlement(operator_id, reservation_id, status, final_amount_uah):
        r = state.get(reservation_id)
        if r is None or r["operator_id"] != operator_id or r["status"] == status:
            return False
        r["status"] = status
        r["final_amount_uah"] = final_amount_uah
        return True

    for name, func in [
        ("get_reservation_by_invoice_id", get_reservation_by_invoice_id),
        ("get_operator_monobank_token_encrypted", get_operator_monobank_token_encrypted),
        ("mark_reservation_hold_confirmed", mark_reservation_hold_confirmed),
        ("get_station", get_station),
        ("record_uah_settlement", record_uah_settlement),
    ]:
        monkeypatch.setattr(repo, name, func)

    monkeypatch.setattr(webhook, "decrypt_secret", lambda enc: enc)

    return state


@pytest.fixture
def bank(monkeypatch, billing):
    """billing.status_calls рахує звернення до get_invoice_status — банк, а не тіло, джерело правди."""
    class FakeBank:
        reply = {"status": "hold"}
        error = None

    async def get_invoice_status(token, invoice_id):
        billing.status_calls += 1
        if FakeBank.error:
            raise FakeBank.error
        return dict(FakeBank.reply)

    async def cancel_invoice(token, invoice_id):
        billing.cancel_calls.append(invoice_id)
        return {"status": "success"}

    monkeypatch.setattr(webhook, "get_invoice_status", get_invoice_status)
    monkeypatch.setattr(webhook, "cancel_invoice", cancel_invoice)
    return FakeBank


@pytest.fixture
def remote_start(monkeypatch, billing):
    class FakeRemoteStart:
        accepted = True
        error = None

    async def remote_start_transaction(operator_id, cp_id, id_tag, connector_id=None):
        billing.remote_start_calls.append((operator_id, cp_id, id_tag))
        if FakeRemoteStart.error is not None:
            raise FakeRemoteStart.error
        return FakeRemoteStart.accepted

    monkeypatch.setattr(webhook, "remote_start_transaction", remote_start_transaction)
    return FakeRemoteStart


class FakeRequest:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode() if payload is not None else b""

    async def body(self):
        return self._body


async def _post(operator_id, payload):
    return await webhook.charging_hold_webhook(operator_id, FakeRequest(payload))


@pytest.fixture
def token_setup(billing):
    billing.tokens[OPERATOR_A] = "encrypted-token-A"
    billing.add_reservation(OPERATOR_A, INVOICE, RESERVATION_ID)
    billing.add_station(OPERATOR_A, station_id=10, cp_id="CP-1")
    return billing


# ---------------------------------------------------------------------------
# 1. Тілу webhook не вірять — лише СВІЖА відповідь банку
# ---------------------------------------------------------------------------

async def test_webhook_does_not_credit_hold_from_body_alone(token_setup, bank, remote_start):
    """Тіло каже все що завгодно — має значення лише те, що скаже банк."""
    bank.reply = {"status": "processing"}  # банк каже: ще НЕ hold

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE, "status": "hold"})

    assert response.status_code == 200
    assert token_setup.get(RESERVATION_ID)["status"] == "awaiting_hold"
    assert remote_start.accepted or True  # RemoteStart не мав викликатись
    assert token_setup.remote_start_calls == []


async def test_webhook_confirms_and_triggers_remote_start_when_bank_says_hold(token_setup, bank, remote_start):
    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert token_setup.confirm_calls == [RESERVATION_ID]
    assert token_setup.get(RESERVATION_ID)["status"] == "pending"
    assert token_setup.remote_start_calls == [(OPERATOR_A, "CP-1", "tag-1")]
    assert token_setup.cancel_calls == []


# ---------------------------------------------------------------------------
# 2. Ізоляція тенантів, невідомий/повторний інвойс
# ---------------------------------------------------------------------------

async def test_webhook_of_another_operator_cannot_confirm_foreign_reservation(token_setup, bank, remote_start):
    response = await _post(OPERATOR_B, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert token_setup.status_calls == 0, "Банк не мали питати про чужий інвойс"
    assert token_setup.get(RESERVATION_ID)["status"] == "awaiting_hold"


async def test_unknown_invoice_answers_quietly(token_setup, bank, remote_start):
    response = await _post(OPERATOR_A, {"invoiceId": "не-існує"})

    assert response.status_code == 200
    assert token_setup.status_calls == 0


async def test_already_processed_reservation_ignores_repeat_webhook(token_setup, bank, remote_start):
    """Резервація вже НЕ 'awaiting_hold' (попередній webhook уже провів перехід) — тихий повтор."""
    token_setup.reservations[(OPERATOR_A, INVOICE)]["status"] = "pending"

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert token_setup.status_calls == 0
    assert token_setup.remote_start_calls == []


@pytest.mark.parametrize("payload", [
    {}, {"invoiceId": ""}, {"status": "hold"}, None,
])
async def test_malformed_bodies_are_ignored_quietly(payload, token_setup, bank, remote_start):
    response = await _post(OPERATOR_A, payload)

    assert response.status_code == 200
    assert token_setup.status_calls == 0


async def test_missing_operator_token_is_handled_quietly(bank, remote_start, billing):
    billing.add_reservation(OPERATOR_A, INVOICE, RESERVATION_ID)
    # Токен НЕ виставлений.

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert billing.status_calls == 0
    assert billing.get(RESERVATION_ID)["status"] == "awaiting_hold"


async def test_bank_unavailable_returns_502_for_retry(token_setup, bank, remote_start):
    from app.services.monobank_acquiring import MonobankError

    bank.error = MonobankError("bank timeout")

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 502
    assert token_setup.get(RESERVATION_ID)["status"] == "awaiting_hold"


# ---------------------------------------------------------------------------
# 3. Ідемпотентність мʼютексу
# ---------------------------------------------------------------------------

async def test_repeated_webhook_does_not_trigger_remote_start_twice(token_setup, bank, remote_start):
    await _post(OPERATOR_A, {"invoiceId": INVOICE})
    await _post(OPERATOR_A, {"invoiceId": INVOICE})
    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert len(token_setup.remote_start_calls) == 1, "Мʼютекс awaiting_hold->pending мав пропустити лише один раз"


# ---------------------------------------------------------------------------
# 4. Герметизація (правка 5 рев'ю): компенсація — НЕГАЙНИЙ cancel_invoice()
# ---------------------------------------------------------------------------

async def test_remote_start_rejected_compensates_with_immediate_cancel(token_setup, bank, remote_start):
    remote_start.accepted = False

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert token_setup.cancel_calls == [INVOICE]
    assert token_setup.get(RESERVATION_ID)["status"] == "cancelled"
    assert token_setup.get(RESERVATION_ID)["final_amount_uah"] == 0


async def test_station_not_connected_compensates_with_immediate_cancel(token_setup, bank, remote_start):
    remote_start.error = ChargePointNotConnected("CP-1")

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert token_setup.cancel_calls == [INVOICE]
    assert token_setup.get(RESERVATION_ID)["status"] == "cancelled"


async def test_station_not_ocpp_compensates_with_immediate_cancel(token_setup, bank, remote_start):
    """Станція без ocpp_charge_point_id — той самий "not_ocpp" клас, що модель A."""
    token_setup.stations[(OPERATOR_A, 10)]["ocpp_charge_point_id"] = None

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert token_setup.cancel_calls == [INVOICE]
    assert token_setup.remote_start_calls == []


async def test_unexpected_exception_compensates_and_reraises(token_setup, bank, remote_start):
    """
    Правка 5 рев'ю: БУДЬ-ЯКИЙ неочікуваний виняток (не лише
    ChargePointNotConnected) компенсується cancel_invoice(), а сам виняток
    перевикидається незміненим (той самий контракт, що
    start_charging_session() у моделі A).
    """
    UNIQUE_MARKER = "ocpp-library-exploded-xyz"
    remote_start.error = RuntimeError(UNIQUE_MARKER)

    with pytest.raises(RuntimeError, match=UNIQUE_MARKER):
        await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert token_setup.cancel_calls == [INVOICE]
    assert token_setup.get(RESERVATION_ID)["status"] == "cancelled"


async def test_compensation_bank_failure_leaves_pending_for_reconcile(token_setup, bank, remote_start, monkeypatch):
    """
    Якщо й сама компенсація (cancel_invoice) впаде — резервація лишається
    'pending', її підбирає крок 2 звірки (docs/plan-3c-ii.md розділ 5).
    """
    from app.services.monobank_acquiring import MonobankError

    remote_start.accepted = False

    async def broken_cancel(token, invoice_id):
        raise MonobankError("bank down right now")

    monkeypatch.setattr(webhook, "cancel_invoice", broken_cancel)

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert token_setup.get(RESERVATION_ID)["status"] == "pending", (
        "Резервація лишається 'pending' — звірка довершить cancel пізніше"
    )
