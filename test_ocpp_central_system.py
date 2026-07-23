"""
Тести OCPP 1.6J Central System — кістяк (Промпт 3a), app/api/ocpp_ws.py.

Тестова "заряджалка" — Starlette TestClient.websocket_connect (in-process
ASGI, БЕЗ живої мережі й заліза), повідомлення — сирі OCPP-J кадри
(`[2, id, action, payload]`/`[3, id, payload]`/`[4, id, code, desc, {}]`),
щоб не тягнути клієнтську частину бібліотеки ocpp в тести (той самий
принцип мінімалізму залежностей, що в решті тестів репо).

Central System тестується ІЗОЛЬОВАНО від решти застосунку: збирається
мінімальний FastAPI лише з ocpp_router, а не через app.main (який тягне
aiogram/Bot() і вимагає BOT_TOKEN). Репозиторій підмінюється фейками —
той самий підхід, що в test_operator_payments.py/test_wallet_topup.py.

Запуск: pytest test_ocpp_central_system.py -v
"""
import base64
import json

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import ocpp_ws
from app.core import crypto
from app.database import operators_repo as repo

CP_ID = "CP-001"
OPERATOR_ID = 1
STATION_ID = 10
PASSWORD = "s3cr3t-shared-ocpp-key"


@pytest.fixture
def encryption_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(crypto.ENV_VAR, key)
    crypto.reset_cache()
    yield key
    crypto.reset_cache()


@pytest.fixture(autouse=True)
def clean_registry():
    """_active_charge_points — модульний глобальний стан; ізолюємо тести один від одного."""
    ocpp_ws._active_charge_points.clear()
    yield
    ocpp_ws._active_charge_points.clear()


class FakeStations:
    def __init__(self):
        self.stations = {}    # cp_id -> dict
        self.operators = {}   # operator_id -> dict
        self.seen_calls = []  # [(operator_id, station_id, status), ...]

    def add_station(self, cp_id, operator_id=OPERATOR_ID, station_id=STATION_ID,
                    mode="ocpp", auth_key_encrypted=None, operator_status="active"):
        self.stations[cp_id] = {
            "id": station_id, "operator_id": operator_id, "mode": mode,
            "auth_key_encrypted": auth_key_encrypted,
        }
        self.operators[operator_id] = {"id": operator_id, "status": operator_status}


@pytest.fixture
def fake_repo(monkeypatch):
    state = FakeStations()

    async def get_station_by_ocpp_charge_point_id(cp_id):
        s = state.stations.get(cp_id)
        if s is None:
            return None
        return {"id": s["id"], "operator_id": s["operator_id"], "mode": s["mode"]}

    async def get_operator(operator_id):
        return state.operators.get(operator_id)

    async def get_station_ocpp_auth_key_encrypted(operator_id, station_id):
        for s in state.stations.values():
            if s["operator_id"] == operator_id and s["id"] == station_id:
                return s["auth_key_encrypted"]
        return None

    async def update_station_ocpp_state(operator_id, station_id, status=None):
        state.seen_calls.append((operator_id, station_id, status))
        return True

    monkeypatch.setattr(repo, "get_station_by_ocpp_charge_point_id",
                        get_station_by_ocpp_charge_point_id)
    monkeypatch.setattr(repo, "get_operator", get_operator)
    monkeypatch.setattr(repo, "get_station_ocpp_auth_key_encrypted",
                        get_station_ocpp_auth_key_encrypted)
    monkeypatch.setattr(repo, "update_station_ocpp_state", update_station_ocpp_state)

    return state


@pytest.fixture
def provisioned(fake_repo, encryption_key):
    """Станція CP_ID, оператор active, коректно налаштований auth-ключ."""
    fake_repo.add_station(CP_ID, auth_key_encrypted=crypto.encrypt_secret(PASSWORD))
    return fake_repo


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ocpp_ws.ocpp_router)
    return TestClient(app)


def _auth_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _connect(client, cp_id, subprotocols=("ocpp1.6",), auth=None):
    headers = dict(auth) if auth else {}
    return client.websocket_connect(f"/ocpp/{cp_id}", subprotocols=list(subprotocols),
                                    headers=headers)


# ---------------------------------------------------------------------------
# 1. Sec-WebSocket-Protocol
# ---------------------------------------------------------------------------

def test_missing_ocpp_subprotocol_is_rejected_before_any_db_lookup(client, provisioned):
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client, CP_ID, subprotocols=["bogus"], auth=_auth_header(CP_ID, PASSWORD)):
            pass
    assert exc.value.code == 1002
    assert provisioned.seen_calls == []
    assert CP_ID not in ocpp_ws._active_charge_points


def test_correct_subprotocol_is_accepted(client, provisioned):
    with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)) as ws:
        assert ws.accepted_subprotocol == "ocpp1.6"


# ---------------------------------------------------------------------------
# 2. Ідентичність станції / оператора (усі варіанти -> той самий код 1008)
# ---------------------------------------------------------------------------

def test_unknown_charge_point_id_is_rejected(client, provisioned):
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client, "unknown-cp", auth=_auth_header("unknown-cp", "whatever")):
            pass
    assert exc.value.code == 1008


def test_station_not_in_ocpp_mode_is_rejected(client, fake_repo, encryption_key):
    fake_repo.add_station(CP_ID, mode="manual", auth_key_encrypted=crypto.encrypt_secret(PASSWORD))
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)):
            pass
    assert exc.value.code == 1008


def test_inactive_operator_is_rejected(client, fake_repo, encryption_key):
    fake_repo.add_station(CP_ID, operator_status="pending",
                          auth_key_encrypted=crypto.encrypt_secret(PASSWORD))
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)):
            pass
    assert exc.value.code == 1008


def test_suspended_operator_is_rejected(client, fake_repo, encryption_key):
    fake_repo.add_station(CP_ID, operator_status="suspended",
                          auth_key_encrypted=crypto.encrypt_secret(PASSWORD))
    with pytest.raises(WebSocketDisconnect):
        with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)):
            pass


def test_station_without_configured_auth_key_is_rejected(client, fake_repo, encryption_key):
    fake_repo.add_station(CP_ID, auth_key_encrypted=None)
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client, CP_ID, auth=_auth_header(CP_ID, "whatever")):
            pass
    assert exc.value.code == 1008


# ---------------------------------------------------------------------------
# 3. Basic Auth (OCPP security profile 1)
# ---------------------------------------------------------------------------

def test_missing_authorization_header_is_rejected(client, provisioned):
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client, CP_ID):
            pass
    assert exc.value.code == 1008


def test_non_basic_authorization_scheme_is_rejected(client, provisioned):
    with pytest.raises(WebSocketDisconnect):
        with _connect(client, CP_ID, auth={"Authorization": "Bearer sometoken"}):
            pass


def test_wrong_password_is_rejected(client, provisioned):
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client, CP_ID, auth=_auth_header(CP_ID, "wrong-password")):
            pass
    assert exc.value.code == 1008
    assert provisioned.seen_calls == []


def test_username_not_matching_url_cp_id_is_rejected(client, provisioned):
    """
    Захист від переплутаних облікових даних: навіть якщо пароль правильний,
    Basic Auth username МАЄ збігатись із cp_id з URL.
    """
    with pytest.raises(WebSocketDisconnect):
        with _connect(client, CP_ID, auth=_auth_header("some-other-cp", PASSWORD)):
            pass


def test_credentials_of_another_station_do_not_authenticate_this_one(client, fake_repo, encryption_key):
    """
    Тенантна ізоляція на рівні OCPP-хендшейку: пароль станції Б не підходить
    для cp_id станції А, навіть якщо обидві належать тому самому чи різним
    операторам — кожен ocpp_charge_point_id має власний незалежний ключ.
    """
    fake_repo.add_station("CP-A", operator_id=1, station_id=10,
                          auth_key_encrypted=crypto.encrypt_secret("password-A"))
    fake_repo.add_station("CP-B", operator_id=2, station_id=20,
                          auth_key_encrypted=crypto.encrypt_secret("password-B"))

    with pytest.raises(WebSocketDisconnect):
        with _connect(client, "CP-A", auth=_auth_header("CP-A", "password-B")):
            pass


def test_encryption_key_missing_is_handled_without_crashing(client, fake_repo, monkeypatch):
    """
    ENCRYPTION_KEY не заданий у середовищі (не викликали фікстуру
    encryption_key) — decrypt_secret кидає EncryptionKeyMissing; з'єднання
    відхиляється так само тихо, а не падає з necaught винятком.
    """
    monkeypatch.delenv(crypto.ENV_VAR, raising=False)
    crypto.reset_cache()
    fake_repo.add_station(CP_ID, auth_key_encrypted="щось-незашифроване-цим-ключем")
    with pytest.raises(WebSocketDisconnect) as exc:
        with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)):
            pass
    assert exc.value.code == 1008


# ---------------------------------------------------------------------------
# 4. Успішне з'єднання: BootNotification / Heartbeat / StatusNotification
# ---------------------------------------------------------------------------

def test_valid_connection_handles_all_three_messages_and_persists_state(client, provisioned):
    with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)) as ws:
        assert CP_ID in ocpp_ws._active_charge_points

        ws.send_text(json.dumps([2, "1", "BootNotification",
                                 {"chargePointVendor": "Acme", "chargePointModel": "X1"}]))
        boot = json.loads(ws.receive_text())
        assert boot[0] == 3, f"очікувався CallResult, отримано {boot}"
        assert boot[2]["status"] == "Accepted"
        assert boot[2]["interval"] == ocpp_ws.HEARTBEAT_INTERVAL_SECONDS
        assert "currentTime" in boot[2]

        ws.send_text(json.dumps([2, "2", "Heartbeat", {}]))
        heartbeat = json.loads(ws.receive_text())
        assert heartbeat[0] == 3
        assert "currentTime" in heartbeat[2]

        ws.send_text(json.dumps([2, "3", "StatusNotification",
                                 {"connectorId": 1, "errorCode": "NoError", "status": "Available"}]))
        status = json.loads(ws.receive_text())
        assert status == [3, "3", {}]

    # реєстр і DB-записи
    assert CP_ID not in ocpp_ws._active_charge_points, "з'єднання не прибрано після дисконекту"
    assert len(provisioned.seen_calls) == 3
    assert provisioned.seen_calls[0] == (OPERATOR_ID, STATION_ID, None)   # Boot
    assert provisioned.seen_calls[1] == (OPERATOR_ID, STATION_ID, None)   # Heartbeat
    assert provisioned.seen_calls[2] == (OPERATOR_ID, STATION_ID, "Available")  # StatusNotification


# ---------------------------------------------------------------------------
# 5. Поза обсягом 3a (Authorize тощо) і криві повідомлення не валять з'єднання
# ---------------------------------------------------------------------------

def test_unimplemented_action_returns_call_error_without_breaking_connection(client, provisioned):
    """
    Authorize/StartTransaction/... — Промпт 3b, тут немає @on-хендлера.
    Бібліотека сама повертає CallError 'NotImplemented' — з'єднання й далі
    живе, наступне повідомлення обробляється нормально.
    """
    with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(json.dumps([2, "1", "Authorize", {"idTag": "abc123"}]))
        resp = json.loads(ws.receive_text())
        assert resp[0] == 4, f"очікувався CallError, отримано {resp}"
        assert resp[2] == "NotImplemented"

        ws.send_text(json.dumps([2, "2", "Heartbeat", {}]))
        hb = json.loads(ws.receive_text())
        assert hb[0] == 3


def test_malformed_message_is_ignored_without_breaking_the_read_loop(client, provisioned):
    with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text("це взагалі не JSON, а тим паче не OCPP-кадр")
        # немає відповіді на криве повідомлення (route_message лише логує
        # й повертається) — доводимо, що read-цикл і далі живий наступним
        # валідним повідомленням.
        ws.send_text(json.dumps([2, "1", "Heartbeat", {}]))
        resp = json.loads(ws.receive_text())
        assert resp[0] == 3


# ---------------------------------------------------------------------------
# 6. Дисконект/очищення реєстру — одна відвала станція не валить інші
# ---------------------------------------------------------------------------

def test_abrupt_disconnect_still_cleans_up_the_registry(client, provisioned):
    with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)) as ws:
        assert CP_ID in ocpp_ws._active_charge_points
        ws.send_text(json.dumps([2, "1", "Heartbeat", {}]))
        json.loads(ws.receive_text())
        # клієнт виходить з `with` без "чемного" close-хендшейку по суті —
        # TestClient все одно шле дисконект, сервер має прибрати за собою.

    assert CP_ID not in ocpp_ws._active_charge_points


def test_two_charge_points_do_not_interfere_with_each_others_registry_entry(
        client, fake_repo, encryption_key):
    fake_repo.add_station("CP-A", operator_id=1, station_id=10,
                          auth_key_encrypted=crypto.encrypt_secret("pw-a"))
    fake_repo.add_station("CP-B", operator_id=2, station_id=20,
                          auth_key_encrypted=crypto.encrypt_secret("pw-b"))

    with _connect(client, "CP-A", auth=_auth_header("CP-A", "pw-a")) as ws_a:
        assert "CP-A" in ocpp_ws._active_charge_points
        with _connect(client, "CP-B", auth=_auth_header("CP-B", "pw-b")) as ws_b:
            assert "CP-A" in ocpp_ws._active_charge_points
            assert "CP-B" in ocpp_ws._active_charge_points
        # CP-B відключилась — CP-A лишається живою і в реєстрі.
        assert "CP-A" in ocpp_ws._active_charge_points
        assert "CP-B" not in ocpp_ws._active_charge_points

        ws_a.send_text(json.dumps([2, "1", "Heartbeat", {}]))
        resp = json.loads(ws_a.receive_text())
        assert resp[0] == 3, "CP-A має лишатись робочою після дисконекту CP-B"

    assert "CP-A" not in ocpp_ws._active_charge_points


def test_stale_connection_cleanup_does_not_evict_a_reconnected_charge_point(client, provisioned):
    """
    Регресія: finally у ocpp_websocket() видаляє запис з реєстру лише якщо
    він і досі вказує на ЦЕ з'єднання (`is charge_point`), а не безумовним
    pop(cp_id, None). Симулюємо гонку "перепідключення" вручну: поки перше
    з'єднання ще активне, підміняємо його запис у реєстрі на маркер іншого
    (нового) з'єднання — саме так виглядав би реєстр, якби станція встигла
    перепідключитись ДО того, як старе з'єднання дійшло до свого finally.
    Дисконект старого з'єднання (вихід з `with`) НЕ має стерти чужий запис.
    """
    with _connect(client, CP_ID, auth=_auth_header(CP_ID, PASSWORD)):
        assert CP_ID in ocpp_ws._active_charge_points
        stale_marker = object()
        ocpp_ws._active_charge_points[CP_ID] = stale_marker

    assert ocpp_ws._active_charge_points.get(CP_ID) is stale_marker, (
        "Дисконект старого з'єднання стер запис 'нового' підключення — "
        "безумовний pop(cp_id, None) саме так і зробив би"
    )
