"""
Тести OCPP 1.6J транзакцій + метрингу (Промпт 3b), app/api/ocpp_ws.py.

Два рівні тестів, свідомо розділені:

1. WS-рівень (Authorize/StartTransaction/StopTransaction/MeterValues) —
   сирі OCPP-J кадри через Starlette TestClient.websocket_connect
   (in-process ASGI, БЕЗ живої мережі), той самий підхід, що
   test_ocpp_central_system.py. Це однонаправлені CP -> CS виклики,
   TestClient з ними чудово справляється.

2. RemoteStart/StopTransaction (CS -> CP) — юніт-тести на
   remote_start_transaction()/remote_stop_transaction() з фейковим
   "підключеним" ChargePoint (лише .operator_id + .call()), а НЕ через
   TestClient: charge_point.call() всередині чекає відповідь через
   asyncio.Queue, привʼязану до event loop, на якому був створений
   ChargePoint (усередині WS-роута сервера) — викликати її з ІНШОГО event
   loop (яким був би тестовий) технічно ризиковано/неможливо через
   TestClient. Повний РЕАКТИВНИЙ раунд-тріп (RemoteStart -> симулятор сам
   шле StartTransaction -> MeterValues -> Stop) — окремо, живим прогоном
   на реальному uvicorn (два справжні процеси/event loop, звичайний TCP) —
   див. review_prompt3b.md, там же відтворювані логи.

Репозиторій підмінюється фейками (той самий підхід, що test_wallet_topup.py
/test_ocpp_central_system.py).

Запуск: pytest test_ocpp_transactions.py -v
"""
import base64
import json
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ocpp.v16 import call_result
from ocpp.v16.enums import RemoteStartStopStatus

from app.api import ocpp_ws
from app.core import crypto
from app.database import operators_repo as repo

CP_ID = "CP-001"
OPERATOR_A = 1
OPERATOR_B = 2
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
    ocpp_ws._active_charge_points.clear()
    yield
    ocpp_ws._active_charge_points.clear()


class FakeOcppState:
    """
    Мінімальна модель operator_sessions у пам'яті — відтворює саме ті
    інваріанти, які в проді тримає БД (одна 'charging'-сесія на станцію
    з ocpp_transaction_id, мʼютекс на StopTransaction), щоб тести
    перевіряли реальну поведінку хендлерів, а не самі фейки.
    """

    def __init__(self):
        self.stations = {}     # cp_id -> dict
        self.operators = {}    # operator_id -> dict
        self.sessions = {}     # transaction_id -> dict
        self.reservations = {}  # reservation_id -> dict (Промпт 3c-i)
        self.release_calls = []  # [(reservation_id, remainder, user_id), ...]
        self._next_id = 100
        self.ocpp_state_calls = []  # [(operator_id, station_id, status), ...]

    def add_station(self, cp_id, operator_id=OPERATOR_A, station_id=STATION_ID,
                    mode="ocpp", auth_key_encrypted=None, operator_status="active"):
        self.stations[cp_id] = {
            "id": station_id, "operator_id": operator_id, "mode": mode,
            "auth_key_encrypted": auth_key_encrypted,
        }
        self.operators[operator_id] = {"id": operator_id, "status": operator_status}

    def add_open_session(self, operator_id, station_id, transaction_id, meter_start_wh):
        self.sessions[transaction_id] = {
            "id": transaction_id, "operator_id": operator_id, "station_id": station_id,
            "status": "charging", "ocpp_transaction_id": transaction_id,
            "meter_start_wh": meter_start_wh, "meter_stop_wh": None, "kwh": None,
        }

    def add_reservation(self, reservation_id, operator_id, station_id, user_id,
                        reserved_kwh, id_tag, status="pending", operator_session_id=None):
        self.reservations[reservation_id] = {
            "id": reservation_id, "operator_id": operator_id, "station_id": station_id,
            "user_id": user_id, "payment_method": "kwh", "reserved_kwh": reserved_kwh,
            "id_tag": id_tag, "status": status, "operator_session_id": operator_session_id,
        }


@pytest.fixture
def fake_repo(monkeypatch):
    state = FakeOcppState()

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
        state.ocpp_state_calls.append((operator_id, station_id, status))
        return True

    async def start_ocpp_transaction(operator_id, station_id, meter_start_wh, started_at):
        for s in state.sessions.values():
            if (s["operator_id"] == operator_id and s["station_id"] == station_id
                    and s["status"] == "charging" and s["ocpp_transaction_id"] is not None):
                return s["id"], s["ocpp_transaction_id"], False
        state._next_id += 1
        tid = state._next_id
        state.sessions[tid] = {
            "id": tid, "operator_id": operator_id, "station_id": station_id,
            "status": "charging", "ocpp_transaction_id": tid,
            "meter_start_wh": meter_start_wh, "meter_stop_wh": None, "kwh": None,
        }
        return tid, tid, True

    async def get_session_by_ocpp_transaction_id(operator_id, transaction_id):
        s = state.sessions.get(transaction_id)
        if s is None or s["operator_id"] != operator_id:
            return None
        return s

    async def complete_ocpp_transaction(operator_id, transaction_id, kwh, meter_stop_wh, ended_at):
        s = state.sessions.get(transaction_id)
        if s is None or s["operator_id"] != operator_id or s["status"] == "completed":
            return False
        s["status"] = "completed"
        s["kwh"] = kwh
        s["meter_stop_wh"] = meter_stop_wh
        s["ended_at"] = ended_at
        return True

    async def get_reservation_by_id_tag(id_tag):
        for r in state.reservations.values():
            if r["id_tag"] == id_tag:
                return dict(r)
        return None

    async def activate_reservation(operator_id, reservation_id, operator_session_id):
        r = state.reservations.get(reservation_id)
        if r is None or r["operator_id"] != operator_id or r["status"] != "pending":
            return False
        r["status"] = "active"
        r["operator_session_id"] = operator_session_id
        return True

    async def get_reservation_by_session_id(operator_id, operator_session_id):
        for r in state.reservations.values():
            if r["operator_id"] == operator_id and r["operator_session_id"] == operator_session_id:
                return dict(r)
        return None

    async def complete_ocpp_transaction_and_release(operator_id, transaction_id, kwh, meter_stop_wh,
                                                     ended_at, reservation_id, reserved_kwh, user_id):
        became_completed = await complete_ocpp_transaction(
            operator_id, transaction_id, kwh, meter_stop_wh, ended_at,
        )
        if not became_completed:
            return False
        remainder = reserved_kwh if kwh is None else reserved_kwh - kwh
        if remainder > 0:
            state.release_calls.append((reservation_id, remainder, user_id))
        r = state.reservations.get(reservation_id)
        if r is not None:
            r["status"] = "finalized"
        return True

    for name, func in [
        ("get_station_by_ocpp_charge_point_id", get_station_by_ocpp_charge_point_id),
        ("get_operator", get_operator),
        ("get_station_ocpp_auth_key_encrypted", get_station_ocpp_auth_key_encrypted),
        ("update_station_ocpp_state", update_station_ocpp_state),
        ("start_ocpp_transaction", start_ocpp_transaction),
        ("get_session_by_ocpp_transaction_id", get_session_by_ocpp_transaction_id),
        ("complete_ocpp_transaction", complete_ocpp_transaction),
        ("get_reservation_by_id_tag", get_reservation_by_id_tag),
        ("activate_reservation", activate_reservation),
        ("get_reservation_by_session_id", get_reservation_by_session_id),
        ("complete_ocpp_transaction_and_release", complete_ocpp_transaction_and_release),
    ]:
        monkeypatch.setattr(repo, name, func)

    return state


@pytest.fixture
def provisioned(fake_repo, encryption_key):
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


def _connect(client, cp_id, auth):
    return client.websocket_connect(f"/ocpp/{cp_id}", subprotocols=["ocpp1.6"], headers=dict(auth))


def _call(unique_id, action, payload):
    return json.dumps([2, unique_id, action, payload])


# ---------------------------------------------------------------------------
# Authorize — завжди Accepted (без перевірки балансу, Промпт 3c)
# ---------------------------------------------------------------------------

def test_authorize_is_always_accepted(client, provisioned):
    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "Authorize", {"idTag": "any-tag-at-all"}))
        resp = json.loads(ws.receive_text())
        assert resp == [3, "1", {"idTagInfo": {"status": "Accepted"}}]


# ---------------------------------------------------------------------------
# StartTransaction — створення + ідемпотентність ретраю
# ---------------------------------------------------------------------------

def test_start_transaction_opens_a_charging_session(client, provisioned):
    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StartTransaction", {
            "connectorId": 1, "idTag": "tag1", "meterStart": 1000,
            "timestamp": "2026-07-24T10:00:00Z",
        }))
        resp = json.loads(ws.receive_text())

    assert resp[0] == 3
    assert resp[2]["idTagInfo"]["status"] == "Accepted"
    transaction_id = resp[2]["transactionId"]
    assert transaction_id > 0

    session = provisioned.sessions[transaction_id]
    assert session["status"] == "charging"
    assert session["meter_start_wh"] == 1000


def test_repeated_start_transaction_does_not_open_a_second_session(client, provisioned):
    """
    Ретрай StartTransaction (станція не отримала ack): другий виклик
    повертає ТОЙ САМИЙ transactionId, у стані лишається рівно одна сесія.
    """
    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StartTransaction", {
            "connectorId": 1, "idTag": "tag1", "meterStart": 1000,
            "timestamp": "2026-07-24T10:00:00Z",
        }))
        first = json.loads(ws.receive_text())

        ws.send_text(_call("2", "StartTransaction", {
            "connectorId": 1, "idTag": "tag1", "meterStart": 1000,
            "timestamp": "2026-07-24T10:00:05Z",
        }))
        second = json.loads(ws.receive_text())

    assert first[2]["transactionId"] == second[2]["transactionId"]
    assert len(provisioned.sessions) == 1, "Ретрай StartTransaction створив другу сесію"


# ---------------------------------------------------------------------------
# StopTransaction — обрахунок kWh, ідемпотентність, невідомий transactionId
# ---------------------------------------------------------------------------

def test_stop_transaction_computes_kwh_and_completes_session(client, provisioned):
    provisioned.add_open_session(OPERATOR_A, STATION_ID, transaction_id=555, meter_start_wh=1000)

    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StopTransaction", {
            "meterStop": 16000, "timestamp": "2026-07-24T10:30:00Z", "transactionId": 555,
        }))
        resp = json.loads(ws.receive_text())

    assert resp == [3, "1", {"idTagInfo": {"status": "Accepted"}}]
    session = provisioned.sessions[555]
    assert session["status"] == "completed"
    assert str(session["kwh"]) == "15.000", "(16000-1000)/1000 = 15.000 кВт·год"
    assert session["meter_stop_wh"] == 16000


def test_repeated_stop_transaction_does_not_double_count_kwh(client, provisioned):
    provisioned.add_open_session(OPERATOR_A, STATION_ID, transaction_id=555, meter_start_wh=1000)

    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StopTransaction", {
            "meterStop": 16000, "timestamp": "2026-07-24T10:30:00Z", "transactionId": 555,
        }))
        json.loads(ws.receive_text())

        # Ретрай з іншим (пізнішим) meterStop — станція б надіслала те саме
        # значення, але навіть якби відрізнялось, kwh уже записаних НЕ має
        # перезаписатись.
        ws.send_text(_call("2", "StopTransaction", {
            "meterStop": 99999, "timestamp": "2026-07-24T10:30:05Z", "transactionId": 555,
        }))
        second = json.loads(ws.receive_text())

    assert second == [3, "2", {"idTagInfo": {"status": "Accepted"}}]
    session = provisioned.sessions[555]
    assert str(session["kwh"]) == "15.000", "Повторний StopTransaction перерахував kWh"
    assert session["meter_stop_wh"] == 16000, "Повторний StopTransaction перезаписав meter_stop_wh"


def test_stop_transaction_without_start_transaction_is_graceful(client, provisioned):
    """
    StopTransaction для transactionId, якого немає в БД (станція рестартувала
    й "забула" стан, або зіпсований transactionId) — не крашимось, сесію
    не вигадуємо, усе одно чемно відповідаємо CP.
    """
    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StopTransaction", {
            "meterStop": 5000, "timestamp": "2026-07-24T10:30:00Z", "transactionId": 999999,
        }))
        resp = json.loads(ws.receive_text())

    assert resp == [3, "1", {"idTagInfo": {"status": "Accepted"}}]
    assert 999999 not in provisioned.sessions


@pytest.mark.parametrize("meter_start_wh,meter_stop", [
    (5000, 3000),        # від'ємна дельта (лічильник "пішов назад")
    (0, 10_000_000),     # 10 000 кВт·год за одну сесію — абсурд
])
def test_stop_transaction_guards_against_absurd_delta(client, provisioned, meter_start_wh, meter_stop):
    provisioned.add_open_session(OPERATOR_A, STATION_ID, transaction_id=555, meter_start_wh=meter_start_wh)

    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StopTransaction", {
            "meterStop": meter_stop, "timestamp": "2026-07-24T10:30:00Z", "transactionId": 555,
        }))
        resp = json.loads(ws.receive_text())

    # Сесію все одно закрито (інакше зависла б 'charging' назавжди й
    # заблокувала наступний старт на цій станції), але БЕЗ вигаданого kWh.
    assert resp == [3, "1", {"idTagInfo": {"status": "Accepted"}}]
    session = provisioned.sessions[555]
    assert session["status"] == "completed"
    assert session["kwh"] is None, "Абсурдна дельта не мала записатись як kWh"


# ---------------------------------------------------------------------------
# MeterValues — з transactionId і без (лише телеметрія, не білінг)
# ---------------------------------------------------------------------------

def test_meter_values_with_known_transaction_id_is_acknowledged(client, provisioned):
    provisioned.add_open_session(OPERATOR_A, STATION_ID, transaction_id=555, meter_start_wh=1000)

    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "MeterValues", {
            "connectorId": 1, "transactionId": 555,
            "meterValue": [{"timestamp": "2026-07-24T10:05:00Z",
                           "sampledValue": [{"value": "2500"}]}],
        }))
        resp = json.loads(ws.receive_text())

    assert resp == [3, "1", {}]
    # Сесія й далі 'charging' — MeterValues нічого не змінює в білінгу.
    assert provisioned.sessions[555]["status"] == "charging"
    assert provisioned.sessions[555]["kwh"] is None


def test_meter_values_with_unknown_transaction_id_does_not_crash(client, provisioned):
    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "MeterValues", {
            "connectorId": 1, "transactionId": 424242,
            "meterValue": [{"timestamp": "2026-07-24T10:05:00Z",
                           "sampledValue": [{"value": "2500"}]}],
        }))
        resp = json.loads(ws.receive_text())
    assert resp == [3, "1", {}]


def test_meter_values_without_transaction_id_is_acknowledged_and_ignored(client, provisioned):
    """Clock-aligned періодичні покази поза транзакцією — 3b лише логує/ігнорує."""
    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "MeterValues", {
            "connectorId": 1,
            "meterValue": [{"timestamp": "2026-07-24T10:05:00Z",
                           "sampledValue": [{"value": "2500"}]}],
        }))
        resp = json.loads(ws.receive_text())
    assert resp == [3, "1", {}]


# ---------------------------------------------------------------------------
# Резервації (Промпт 3c-i) — прив'язка на StartTransaction, звільнення
# залишку на StopTransaction. Резервація, якої нема / з чужим idTag / не
# 'pending' — сесія веде себе точно як у 3b (жодного hold/release).
# ---------------------------------------------------------------------------

RESERVATION_ID = 42


def test_start_transaction_activates_matching_pending_reservation(client, provisioned):
    provisioned.add_reservation(
        RESERVATION_ID, OPERATOR_A, STATION_ID, user_id=777,
        reserved_kwh=Decimal("20.000"), id_tag="reserved-tag",
    )

    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StartTransaction", {
            "connectorId": 1, "idTag": "reserved-tag", "meterStart": 1000,
            "timestamp": "2026-07-24T10:00:00Z",
        }))
        resp = json.loads(ws.receive_text())

    transaction_id = resp[2]["transactionId"]
    reservation = provisioned.reservations[RESERVATION_ID]
    assert reservation["status"] == "active"
    assert reservation["operator_session_id"] == transaction_id


def test_start_transaction_with_unknown_id_tag_does_not_touch_reservations(client, provisioned):
    """idTag, що не збігається з жодною резервацією — поведінка 1:1 як у 3b."""
    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StartTransaction", {
            "connectorId": 1, "idTag": "walk-up-no-resv", "meterStart": 1000,
            "timestamp": "2026-07-24T10:00:00Z",
        }))
        resp = json.loads(ws.receive_text())

    assert resp[2]["idTagInfo"]["status"] == "Accepted"
    assert provisioned.reservations == {}


def test_start_transaction_retry_does_not_reactivate_reservation(client, provisioned):
    """
    Ретрай StartTransaction (is_new=False) не заходить у гілку активації
    резервації вдруге — сесія та резервація лишаються прив'язаними лише
    з першого разу.
    """
    provisioned.add_reservation(
        RESERVATION_ID, OPERATOR_A, STATION_ID, user_id=777,
        reserved_kwh=Decimal("20.000"), id_tag="reserved-tag",
    )

    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StartTransaction", {
            "connectorId": 1, "idTag": "reserved-tag", "meterStart": 1000,
            "timestamp": "2026-07-24T10:00:00Z",
        }))
        json.loads(ws.receive_text())

        ws.send_text(_call("2", "StartTransaction", {
            "connectorId": 1, "idTag": "reserved-tag", "meterStart": 1000,
            "timestamp": "2026-07-24T10:00:05Z",
        }))
        second = json.loads(ws.receive_text())

    assert second[2]["idTagInfo"]["status"] == "Accepted"
    assert provisioned.reservations[RESERVATION_ID]["status"] == "active"


def test_stop_transaction_releases_unused_remainder_of_active_reservation(client, provisioned):
    provisioned.add_reservation(
        RESERVATION_ID, OPERATOR_A, STATION_ID, user_id=777,
        reserved_kwh=Decimal("20.000"), id_tag="reserved-tag",
        status="active", operator_session_id=555,
    )
    provisioned.add_open_session(OPERATOR_A, STATION_ID, transaction_id=555, meter_start_wh=1000)

    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StopTransaction", {
            "meterStop": 16000, "timestamp": "2026-07-24T10:30:00Z", "transactionId": 555,
        }))
        resp = json.loads(ws.receive_text())

    assert resp == [3, "1", {"idTagInfo": {"status": "Accepted"}}]
    session = provisioned.sessions[555]
    assert str(session["kwh"]) == "15.000"

    assert provisioned.reservations[RESERVATION_ID]["status"] == "finalized"
    assert provisioned.release_calls == [(RESERVATION_ID, Decimal("5.000"), 777)]


def test_stop_transaction_with_absurd_delta_releases_the_whole_reservation(client, provisioned):
    """kwh=None (абсурдна дельта) -> звільняємо ВЕСЬ резерв, не вигадуємо спожите."""
    provisioned.add_reservation(
        RESERVATION_ID, OPERATOR_A, STATION_ID, user_id=777,
        reserved_kwh=Decimal("20.000"), id_tag="reserved-tag",
        status="active", operator_session_id=555,
    )
    provisioned.add_open_session(OPERATOR_A, STATION_ID, transaction_id=555, meter_start_wh=5000)

    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StopTransaction", {
            "meterStop": 3000, "timestamp": "2026-07-24T10:30:00Z", "transactionId": 555,
        }))
        resp = json.loads(ws.receive_text())

    assert resp == [3, "1", {"idTagInfo": {"status": "Accepted"}}]
    assert provisioned.sessions[555]["kwh"] is None
    assert provisioned.reservations[RESERVATION_ID]["status"] == "finalized"
    assert provisioned.release_calls == [(RESERVATION_ID, Decimal("20.000"), 777)]


def test_stop_transaction_without_reservation_behaves_exactly_like_3b(client, provisioned):
    """Немає прив'язаної резервації (звичайна не-передоплачена сесія) — без release-виклику."""
    provisioned.add_open_session(OPERATOR_A, STATION_ID, transaction_id=555, meter_start_wh=1000)

    with _connect(client, CP_ID, _auth_header(CP_ID, PASSWORD)) as ws:
        ws.send_text(_call("1", "StopTransaction", {
            "meterStop": 16000, "timestamp": "2026-07-24T10:30:00Z", "transactionId": 555,
        }))
        json.loads(ws.receive_text())

    assert provisioned.release_calls == []
    assert str(provisioned.sessions[555]["kwh"]) == "15.000"


# ---------------------------------------------------------------------------
# RemoteStart/StopTransaction (CS -> CP) — юніт-тести на фейковому "з'єднанні"
# ---------------------------------------------------------------------------

class FakeConnectedChargePoint:
    """
    Мінімальна заглушка "підключеної" станції для remote_start_transaction/
    remote_stop_transaction: лише .operator_id (звіряється guard-ом) і
    .call() (записує запит, повертає заготовлену відповідь). Реального
    ChargePoint/WS тут немає — .call() у бібліотеці чекає відповідь через
    asyncio.Queue, привʼязану до event loop сервера, тому справжній
    ChargePoint у юніт-тесті використовувати не можна (див. докстрінг
    файлу) — повний раунд-тріп перевірений живим прогоном (review_prompt3b.md).
    """

    def __init__(self, operator_id, response):
        self.operator_id = operator_id
        self.calls = []
        self._response = response

    async def call(self, payload):
        self.calls.append(payload)
        return self._response


async def test_remote_start_transaction_sends_the_right_request():
    fake_cp = FakeConnectedChargePoint(
        OPERATOR_A, call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted),
    )
    ocpp_ws._active_charge_points[CP_ID] = fake_cp

    accepted = await ocpp_ws.remote_start_transaction(OPERATOR_A, CP_ID, "tag1", connector_id=1)

    assert accepted is True
    assert len(fake_cp.calls) == 1
    assert fake_cp.calls[0].id_tag == "tag1"
    assert fake_cp.calls[0].connector_id == 1


async def test_remote_start_transaction_reports_rejection():
    fake_cp = FakeConnectedChargePoint(
        OPERATOR_A, call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected),
    )
    ocpp_ws._active_charge_points[CP_ID] = fake_cp

    accepted = await ocpp_ws.remote_start_transaction(OPERATOR_A, CP_ID, "tag1")
    assert accepted is False


async def test_remote_start_transaction_raises_when_station_not_connected():
    with pytest.raises(ocpp_ws.ChargePointNotConnected):
        await ocpp_ws.remote_start_transaction(OPERATOR_A, "not-connected-cp", "tag1")


async def test_remote_start_transaction_raises_for_a_different_operators_station():
    """
    Тенант-ізоляція: оператор Б не може дистанційно стартувати станцію
    оператора А, навіть точно знаючи її cp_id.
    """
    fake_cp = FakeConnectedChargePoint(
        OPERATOR_A, call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted),
    )
    ocpp_ws._active_charge_points[CP_ID] = fake_cp

    with pytest.raises(ocpp_ws.ChargePointNotConnected):
        await ocpp_ws.remote_start_transaction(OPERATOR_B, CP_ID, "tag1")
    assert fake_cp.calls == [], "Запит не мав піти на дріт для чужого оператора"


async def test_remote_stop_transaction_sends_the_right_request():
    fake_cp = FakeConnectedChargePoint(
        OPERATOR_A, call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted),
    )
    ocpp_ws._active_charge_points[CP_ID] = fake_cp

    accepted = await ocpp_ws.remote_stop_transaction(OPERATOR_A, CP_ID, transaction_id=101)

    assert accepted is True
    assert fake_cp.calls[0].transaction_id == 101


async def test_remote_stop_transaction_raises_for_a_different_operators_station():
    fake_cp = FakeConnectedChargePoint(
        OPERATOR_A, call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted),
    )
    ocpp_ws._active_charge_points[CP_ID] = fake_cp
    with pytest.raises(ocpp_ws.ChargePointNotConnected):
        await ocpp_ws.remote_stop_transaction(OPERATOR_B, CP_ID, transaction_id=101)
