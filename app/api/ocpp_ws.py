"""
OCPP 1.6J Central System — кістяк (Промпт 3a).

    WS  /ocpp/{cp_id}

Що вміє (і НАВМИСНО лише це): приймає з'єднання зарядної станції (charge
point), відповідає на BootNotification / Heartbeat / StatusNotification.
Authorize, Start/StopTransaction, MeterValues, RemoteStart/StopTransaction,
прив'язка до сесій і оплата — Промпт 3b/3c, тут немає жодного рядка коду
для них. Бібліотека `ocpp` (mobilityhouse) сама повертає коректний OCPP
CallError 'NotImplemented' для будь-якої дії, для якої немає @on-хендлера
нижче — перевірено емпірично, нічого додатково забороняти не треба.

Стек: один і той самий FastAPI-процес/цикл подій, що й решта застосунку —
жодного нового процесу чи event loop. nginx-проксі на /ocpp (Upgrade/
Connection) — РУЧНИЙ крок на сервері (як HTTPS), не в цьому коді.

Реєстр зарядок: своєї сутності НЕ заведено — перевикористано наявні
operator_stations.ocpp_charge_point_id / get_station_by_ocpp_charge_point_id
(другий свідомий "публічний виняток" з правила ізоляції, задокументований
ще в докстрінгу модуля app/database/operators_repo.py при Промпті 4c: сама
станція автентифікована не через telegram-акаунт оператора, а через
OCPP-ідентичність, тому operator_id тут іде НЕ параметром ззовні, а
результатом пошуку за унікальним ocpp_charge_point_id).

Модель довіри на handshake (ДО websocket.accept() — відмова = з'єднання
взагалі не встановлюється):
  1. Sec-WebSocket-Protocol має пропонувати рівно "ocpp1.6" — інакше
     станція/симулятор технічно не зможе говорити тим самим діалектом.
  2. cp_id має резолвитись у станцію з mode='ocpp', чий оператор active,
     і мати налаштований auth-ключ (OCPP security profile 1 — HTTP Basic
     Auth на upgrade-запиті: username=cp_id, password=спільний секрет,
     Fernet-шифрований у БД тим самим ENCRYPTION_KEY, що й
     operators.monobank_token_encrypted).
  3. Усі варіанти відмови (немає такої станції, не той mode, оператор не
     active, auth не налаштовано, заголовка Authorization немає/невалідний,
     пароль не збігається) закриваються ОДНАКОВО (WS-код 1008, без тексту
     причини на дроті) — той самий анти-оракул принцип, що й "тихий 200" у
     app/api/operator_webhook.py / app/api/wallet_webhook.py: зловмисник не
     має змоги за різницею в поведінці визначити, чи вгадав він хоча б
     існування станції з таким cp_id.

Конкурентність: сам WS-роут FastAPI вже є асинхронним per-з'єднання
таском, яким керує uvicorn — ChargePoint.start() (блокуючий read-цикл
бібліотеки ocpp) просто await-иться прямо тут, без ручного
asyncio.create_task(). try/except/finally навколо гарантує, що з'єднання й
запис у реєстрі прибираються за БУДЬ-ЯКОГО завершення (чистий дисконект,
розрив мережі, помилка) — одна відвала станція не чіпає інші з'єднання й
не лишає висячих тасків.
"""
import base64
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint, call_result
from ocpp.v16.enums import Action, RegistrationStatus

from app.core.crypto import EncryptionKeyMissing, decrypt_secret
from app.database import operators_repo as repo

logger = logging.getLogger(__name__)

ocpp_router = APIRouter()

# Єдиний підпротокол, який ми говоримо. OCPP 1.6J вимагає рівно цей рядок
# у Sec-WebSocket-Protocol — станція/симулятор, що його не пропонує, не
# зможе коректно обмінюватись повідомленнями навіть якби ми прийняли
# з'єднання.
OCPP_SUBPROTOCOL = "ocpp1.6"

# Інтервал (сек.) між Heartbeat, який Central System диктує станції у
# відповіді на BootNotification. 300с (5 хв) — типове значення за
# замовчуванням в екосистемі OCPP 1.6, не наша вигадка.
HEARTBEAT_INTERVAL_SECONDS = 300

# Живі з'єднання: cp_id -> ChargePoint. In-memory, у межах одного процесу —
# для 3a лише для видимості/дебагу; Промпт 3b використає той самий реєстр,
# щоб надсилати команди (RemoteStartTransaction тощо) конкретній зарядці.
# Приберається в finally нижче за БУДЬ-якого завершення з'єднання.
_active_charge_points: dict = {}


class _StarletteWebSocketAdapter:
    """
    Бібліотека ocpp писалась під `websockets` (.recv()/.send(), обидва
    async, str) — той самий контракт, що вимагає ocpp.charge_point.
    ChargePoint._connection (перевірено джерелом бібліотеки: start() робить
    `await self._connection.recv()`, _send() робить `await self._connection.
    send(message)`). Starlette WebSocket називає ці самі операції інакше
    (.receive_text()/.send_text()) — цей клас лише перекладає імена, жодної
    власної логіки.
    """

    def __init__(self, websocket: WebSocket):
        self._websocket = websocket

    async def recv(self) -> str:
        return await self._websocket.receive_text()

    async def send(self, message: str) -> None:
        await self._websocket.send_text(message)


class ChargePoint(OcppChargePoint):
    """
    Central System: рівно три хендлери (Промпт 3a). Усе інше (Authorize,
    Start/StopTransaction, MeterValues, RemoteStart/StopTransaction) —
    свідомо відсутнє; бібліотека сама відповість CallError 'NotImplemented'
    (route_map не міститиме запису для цих Action).
    """

    def __init__(self, cp_id: str, connection, operator_id: int, station_id: int):
        super().__init__(cp_id, connection)
        self.operator_id = operator_id
        self.station_id = station_id

    @on(Action.boot_notification)
    async def on_boot_notification(self, charge_point_vendor, charge_point_model, **kwargs):
        await repo.update_station_ocpp_state(self.operator_id, self.station_id)
        logger.info("🔌 OCPP BootNotification: станція %s (%s %s)",
                    self.id, charge_point_vendor, charge_point_model)
        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=HEARTBEAT_INTERVAL_SECONDS,
            status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)
    async def on_heartbeat(self):
        await repo.update_station_ocpp_state(self.operator_id, self.station_id)
        return call_result.Heartbeat(
            current_time=datetime.now(timezone.utc).isoformat(),
        )

    @on(Action.status_notification)
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        await repo.update_station_ocpp_state(self.operator_id, self.station_id, status=status)
        logger.info("🔌 OCPP StatusNotification: станція %s, конектор %s -> %s",
                    self.id, connector_id, status)
        return call_result.StatusNotification()


def _parse_basic_auth(header_value: str):
    """
    Розбирає заголовок `Authorization: Basic <base64(user:pass)>`.
    Повертає (username, password) або None за БУДЬ-якої невалідності —
    викликач не розрізняє причину (немає заголовка / не Basic / зламаний
    base64 / немає ':'), той самий анти-оракул принцип, що й для решти
    handshake-перевірок нижче.
    """
    if not header_value or not header_value.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header_value[len("basic "):].strip()).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    username, _, password = decoded.partition(":")
    return username, password


@ocpp_router.websocket("/ocpp/{cp_id}")
async def ocpp_websocket(websocket: WebSocket, cp_id: str):
    async def reject(reason: str, code: int = 1008):
        # Один і той самий код/порожня причина на дроті для всіх варіантів
        # відмови — детальна причина лишається ЛИШЕ в серверному логі.
        logger.warning("OCPP WS %s: відхилено (%s)", cp_id, reason)
        await websocket.close(code=code)

    offered = websocket.headers.get("sec-websocket-protocol", "")
    offered_protocols = [p.strip() for p in offered.split(",") if p.strip()]
    if OCPP_SUBPROTOCOL not in offered_protocols:
        await reject(f"клієнт не запропонував підпротокол {OCPP_SUBPROTOCOL} "
                    f"(запропоновано: {offered_protocols})", code=1002)
        return

    station = await repo.get_station_by_ocpp_charge_point_id(cp_id)
    if station is None or station["mode"] != "ocpp":
        await reject("невідома або не-OCPP станція")
        return

    operator = await repo.get_operator(station["operator_id"])
    if operator is None or operator["status"] != "active":
        await reject("оператор станції не активний")
        return

    auth_key_encrypted = await repo.get_station_ocpp_auth_key_encrypted(
        station["operator_id"], station["id"],
    )
    if not auth_key_encrypted:
        await reject("для станції не налаштовано обліковий запис OCPP")
        return

    credentials = _parse_basic_auth(websocket.headers.get("authorization", ""))
    if credentials is None:
        await reject("відсутній або невалідний заголовок Authorization")
        return
    username, password = credentials

    try:
        expected_password = decrypt_secret(auth_key_encrypted)
    except (EncryptionKeyMissing, ValueError) as e:
        logger.error("OCPP WS %s: не вдалося розшифрувати ключ станції: %s", cp_id, e)
        await reject("технічна проблема з обліковими даними")
        return

    # hmac.compare_digest — порівняння за постійний час, щоб довжина
    # співпадіння пароля не витікала через тайминг відповіді.
    if username != cp_id or not hmac.compare_digest(password, expected_password):
        await reject("невірні облікові дані")
        return

    await websocket.accept(subprotocol=OCPP_SUBPROTOCOL)

    charge_point = ChargePoint(
        cp_id, _StarletteWebSocketAdapter(websocket),
        operator_id=station["operator_id"], station_id=station["id"],
    )
    _active_charge_points[cp_id] = charge_point
    logger.info("🔌 OCPP: станція %s (оператор %s) підключилась", cp_id, station["operator_id"])

    try:
        await charge_point.start()
    except WebSocketDisconnect as e:
        logger.info("OCPP WS %s: відключилась (код %s)", cp_id, e.code)
    except Exception as e:
        # Будь-яка інша помилка цього ОДНОГО з'єднання (розрив мережі,
        # неочікуваний виняток у read-циклі бібліотеки) не має чіпати інші
        # активні з'єднання чи сам процес.
        logger.error("OCPP WS %s: помилка з'єднання: %s", cp_id, e)
    finally:
        # Не pop(cp_id, None) навмисно: якщо та сама станція встигла
        # перепідключитись (нове з'єднання вже перезаписало реєстр) ДО
        # того, як цей finally виконався для старого з'єднання, безумовний
        # pop стер би запис НОВОГО з'єднання, а не свій власний.
        if _active_charge_points.get(cp_id) is charge_point:
            del _active_charge_points[cp_id]
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass
