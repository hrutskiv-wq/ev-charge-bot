"""
OCPP 1.6J Central System (Промпт 3a — кістяк; Промпт 3b — транзакції+метринг).

    WS  /ocpp/{cp_id}

3a: приймає з'єднання зарядної станції (charge point), відповідає на
BootNotification / Heartbeat / StatusNotification.

3b (цей файл, додано): Authorize (завжди Accepted — перевірки балансу
НЕМАЄ, це 3c), StartTransaction / StopTransaction / MeterValues, і
команди Central System -> станція RemoteStartTransaction/
RemoteStopTransaction через _active_charge_points. Прив'язка сесії до
водія/оплати, списання балансу, ціна за фактом — Промпт 3c, тут НЕМАЄ
жодного рядка коду для них (сесія завжди без payment_id/user).

Усе, для чого немає @on-хендлера нижче (Reset, DataTransfer,
ChangeConfiguration, і навіть RemoteStart/StopTransaction, якби CP чомусь
надіслав їх ЯК Call нам — за специфікацією це виключно напрям CS -> CP)
далі отримує коректний OCPP CallError 'NotImplemented' від самої
бібліотеки `ocpp` (mobilityhouse) — перевірено емпірично, нічого
додатково забороняти не треба.

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
from decimal import Decimal

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint, call, call_result
from ocpp.v16.datatypes import IdTagInfo
from ocpp.v16.enums import Action, AuthorizationStatus, RegistrationStatus, RemoteStartStopStatus

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

# Захист від абсурдної дельти лічильника на StopTransaction (500 кВт·год —
# з великим запасом понад будь-яку реальну сесію легкового EV чи навіть
# автобуса/вантажівки). Не бізнес-правило, а грубий запобіжник від явно
# зіпсованих/переплутаних показів.
MAX_REASONABLE_SESSION_WH = 500_000

# Живі з'єднання: cp_id -> ChargePoint. In-memory, У МЕЖАХ ОДНОГО ПРОЦЕСУ
# uvicorn — свідоме обмеження (задокументовано ще на рев'ю 3a): якщо
# застосунок колись піде на кілька воркерів/процесів, RemoteStart/Stop
# знайдуть станцію лише якщо вона підключена до ТОГО САМОГО процеса, що
# обробляє команду. Для пілоту (один процес) — ок; переносити на Redis
# pub/sub чи чергу команд, коли постане реальна потреба в кількох воркерах.
# Приберається в finally нижче за БУДЬ-якого завершення з'єднання.
_active_charge_points: dict = {}


class ChargePointNotConnected(RuntimeError):
    """RemoteStart/StopTransaction на cp_id, якого зараз немає серед активних з'єднань."""


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


def _parse_ocpp_timestamp(value: str) -> datetime:
    """
    OCPP-повідомлення несуть час у ISO8601, часто із суфіксом 'Z' замість
    '+00:00' — datetime.fromisoformat() в Python 3.11+ це вже підтримує, але
    нормалізуємо явно, щоб не залежати від точної мінорної версії.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ChargePoint(OcppChargePoint):
    """
    Central System: BootNotification/Heartbeat/StatusNotification (3a) +
    Authorize/StartTransaction/StopTransaction/MeterValues (3b). Усе інше
    (MeterValues.req від CP — оброблено; решта: RemoteStart/StopTransaction
    як ВХІДНІ від CP, DataTransfer, Reset, ChangeConfiguration тощо) —
    свідомо відсутнє; бібліотека сама відповість CallError 'NotImplemented'
    (route_map не міститиме запису для цих Action).

    Прив'язки до водія/оплати НЕМАЄ (Промпт 3c): StartTransaction відкриває
    operator_session без payment_id/user, StopTransaction лише записує
    фактичні кВт·год. Authorize завжди Accepted — перевірки балансу нема.
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

    @on(Action.authorize)
    async def on_authorize(self, id_tag, **kwargs):
        """
        Завжди Accepted — жодної перевірки балансу/allowlist (Промпт 3c).
        Це свідома тимчасова заглушка, не остаточна авторизація.
        """
        return call_result.Authorize(id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        session_id, transaction_id, is_new = await repo.start_ocpp_transaction(
            self.operator_id, self.station_id, meter_start, _parse_ocpp_timestamp(timestamp),
        )
        if session_id is None:
            # Теоретично неможливо: operator_id/station_id тут — результат
            # DB-звіреного handshake (3a), не довільний ввід. Захист про
            # всяк випадок, не крашимось.
            logger.error(
                "OCPP StartTransaction: станція %s (оператор %s) не резолвнулась "
                "у власну станцію під час start_ocpp_transaction — сесію не створено",
                self.id, self.operator_id,
            )
            return call_result.StartTransaction(
                transaction_id=0,
                id_tag_info=IdTagInfo(status=AuthorizationStatus.invalid),
            )

        await repo.update_station_ocpp_state(self.operator_id, self.station_id)
        logger.info(
            "🔌 OCPP StartTransaction: станція %s, сесія #%s, transactionId=%s%s",
            self.id, session_id, transaction_id, "" if is_new else " (ретрай — сесія вже була)",
        )
        return call_result.StartTransaction(
            transaction_id=transaction_id,
            id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted),
        )

    @on(Action.stop_transaction)
    async def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        session = await repo.get_session_by_ocpp_transaction_id(self.operator_id, transaction_id)
        if session is None:
            # Станція рестартувала/перепідключилась і "забула" transactionId,
            # або StopTransaction для чужого/вигаданого id — не вигадуємо
            # сесію, лише логуємо, все одно чемно відповідаємо CP.
            logger.warning(
                "OCPP StopTransaction: невідомий transactionId %s (станція %s) — "
                "сесію не вигадуємо", transaction_id, self.id,
            )
            return call_result.StopTransaction(id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

        if session["status"] == "completed":
            logger.info(
                "OCPP StopTransaction: transactionId %s (сесія #%s) уже завершено — "
                "повтор проігноровано", transaction_id, session["id"],
            )
            return call_result.StopTransaction(id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

        ended_at = _parse_ocpp_timestamp(timestamp)
        meter_start_wh = session["meter_start_wh"]
        delta_wh = (
            Decimal(meter_stop) - Decimal(meter_start_wh)
            if meter_start_wh is not None else None
        )

        if delta_wh is None or delta_wh < 0 or delta_wh > MAX_REASONABLE_SESSION_WH:
            logger.error(
                "OCPP StopTransaction: абсурдна дельта лічильника для сесії #%s "
                "(meter_start=%s, meter_stop=%s) — kWh НЕ записано, потрібен ручний розбір",
                session["id"], meter_start_wh, meter_stop,
            )
            await repo.complete_ocpp_transaction(
                self.operator_id, transaction_id, kwh=None,
                meter_stop_wh=meter_stop, ended_at=ended_at,
            )
            return call_result.StopTransaction(id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

        kwh = (delta_wh / Decimal(1000)).quantize(Decimal("0.001"))
        await repo.complete_ocpp_transaction(
            self.operator_id, transaction_id, kwh=kwh,
            meter_stop_wh=meter_stop, ended_at=ended_at,
        )
        logger.info("🔌 OCPP StopTransaction: станція %s, сесія #%s, %s кВт·год",
                    self.id, session["id"], kwh)
        return call_result.StopTransaction(id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, transaction_id=None, **kwargs):
        """
        Лише проміжна телеметрія (НЕ джерело білінгу — те рахує
        StopTransaction). З transactionId — підтверджуємо, що сесія відома,
        і оновлюємо "станція жива"; без transactionId (clock-aligned
        періодичні покази поза транзакцією) — просто логуємо/ігноруємо.
        """
        if transaction_id is not None:
            session = await repo.get_session_by_ocpp_transaction_id(self.operator_id, transaction_id)
            if session is None:
                logger.warning(
                    "OCPP MeterValues: невідомий transactionId %s (станція %s)",
                    transaction_id, self.id,
                )
            else:
                await repo.update_station_ocpp_state(self.operator_id, self.station_id)
                logger.info("OCPP MeterValues: станція %s, сесія #%s, %d проб(и)",
                            self.id, session["id"], len(meter_value))
        else:
            logger.info(
                "OCPP MeterValues: станція %s, без transactionId (clock-aligned) — "
                "ігноруємо в 3b", self.id,
            )
        return call_result.MeterValues()


async def remote_start_transaction(operator_id: int, cp_id: str, id_tag: str,
                                   connector_id: int = None) -> bool:
    """
    Central System -> станція: RemoteStartTransaction.req. За специфікацією
    1.6 це ДВОКРОКОВО — станція лише підтверджує Accepted/Rejected тут;
    сесію реально відкриває вона сама наступним StartTransaction.req
    (on_start_transaction вище), НЕ ця функція.

    operator_id звіряється з тим, що ChargePoint отримав на DB-звіреному
    handshake (3a) — оператор не може смикати чужу станцію, навіть знаючи
    її cp_id. Немає прив'язки до оплати (Промпт 3c) — викликач сам
    відповідає за те, чому варто стартувати.

    Кидає ChargePointNotConnected, якщо станція зараз не підключена до
    ЦЬОГО процесу (див. обмеження _active_charge_points вище).
    """
    charge_point = _active_charge_points.get(cp_id)
    if charge_point is None or charge_point.operator_id != operator_id:
        raise ChargePointNotConnected(cp_id)
    response = await charge_point.call(
        call.RemoteStartTransaction(id_tag=id_tag, connector_id=connector_id)
    )
    return response.status == RemoteStartStopStatus.accepted


async def remote_stop_transaction(operator_id: int, cp_id: str, transaction_id: int) -> bool:
    """Central System -> станція: RemoteStopTransaction.req. Той самий tenant-guard, що вище."""
    charge_point = _active_charge_points.get(cp_id)
    if charge_point is None or charge_point.operator_id != operator_id:
        raise ChargePointNotConnected(cp_id)
    response = await charge_point.call(
        call.RemoteStopTransaction(transaction_id=transaction_id)
    )
    return response.status == RemoteStartStopStatus.accepted


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
