"""
Сервісне ядро "резерв + RemoteStart" / "RemoteStop" (Промпт 3c-i, бот-
адмінкоманди). Один код для CLI (start_charging_session.py) і бот-команд
(app/handlers/ocpp_admin.py) — раніше цю логіку мав лише скрипт, який
запускається ОКРЕМИМ процесом (docker compose exec) і тому НІКОЛИ не бачить
_active_charge_points живого uvicorn-процесу (де реально висять OCPP-
з'єднання станцій) — RemoteStart з нього завжди падав з
ChargePointNotConnected. Ці функції призначені для виклику IN-PROCESS (з
бот-хендлера, що працює в ТОМУ САМОМУ event loop, що й OCPP WS-роут), де
реєстр реально доступний — саме це і робить /ocpp_start/ocpp_stop
працездатними на проді.

Жодних print()/side-effect'ів під конкретний канал — результат
структурований (ChargingStartResult/ChargingStopResult), форматування під
консоль чи Telegram лишається виклику (start_charging_session.py /
app/handlers/ocpp_admin.py відповідно).
"""
import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.api.ocpp_ws import ChargePointNotConnected, remote_start_transaction, remote_stop_transaction
from app.database import operators_repo as repo

logger = logging.getLogger(__name__)


@dataclass
class ChargingStartResult:
    """
    status:
      "ok"                    — резерв взято, RemoteStart прийнято станцією.
      "unknown_station"       — station_id не належить operator_id.
      "insufficient_balance"  — недостатньо kWh-балансу водія (hold не пройшов).
      "not_ocpp"               — станція не в режимі OCPP (немає ocpp_charge_point_id).
      "not_connected"          — станція зараз не підключена до ЦЬОГО процесу.
      "rejected"                — станція відповіла Rejected на RemoteStartTransaction.

    reservation_id/id_tag — None лише для unknown_station/insufficient_balance
    (резервацію тоді ще не було створено). Для решти негативних статусів
    резервація БУЛА створена, але вже звільнена (release_reservation_hold,
    "cancelled") — hold і release компенсують одне одного, нетто-вплив на
    баланс водія 0.
    """
    status: str
    reservation_id: Optional[int] = None
    id_tag: Optional[str] = None


@dataclass
class ChargingStopResult:
    """
    status: "ok" | "unknown_station" | "no_active_session" | "not_connected" | "rejected"
    transaction_id — None лише для unknown_station/no_active_session.
    """
    status: str
    transaction_id: Optional[int] = None


async def start_charging_session(operator_id: int, station_id: int, user_id: int,
                                 reserved_kwh: Decimal) -> ChargingStartResult:
    """
    Резерв kWh-балансу + RemoteStartTransaction. На КОЖНІЙ гілці відмови
    ПІСЛЯ того, як резервація вже реально створена (not_ocpp/not_connected/
    rejected) — release_reservation_hold(..., "cancelled"), щоб hold водія
    не завис без жодного шансу на зарядку. Баланс тут ніде не чіпається
    напряму — лише через create_charging_reservation()/
    release_reservation_hold(), обидві йдуть через update_user_balance().

    Викликач відповідає за валідацію reserved_kwh (Decimal > 0) ДО виклику
    — тут лише передбачається валідне значення (DB-обмеження reserved_kwh >
    0 лишається останнім рубежем).
    """
    reservation_id, id_tag, error = await repo.create_charging_reservation(
        operator_id, station_id, user_id, reserved_kwh,
    )
    if error is not None:
        return ChargingStartResult(status=error)

    # Від цього рядка й до кінця функції баланс водія вже списано в hold
    # (create_charging_reservation() вище відпрацював). Три await нижче —
    # get_station/remote_start_transaction — можуть впасти НЕ лише
    # ChargePointNotConnected (мережевий збій, таймаут, помилка ocpp-
    # бібліотеки, websockets.ConnectionClosed, asyncio.CancelledError при
    # рестарті контейнера). Будь-який вихід звідси без release лишає hold
    # застряглим і водій тихо втрачає кВт·год — тому весь блок нижче в
    # try/except BaseException (не Exception: CancelledError у Python 3.8+
    # успадковується від BaseException, а скасування задачі рівно тут —
    # цілком реальний сценарій).
    try:
        station = await repo.get_station(operator_id, station_id)
        if station is None or station.get("ocpp_charge_point_id") is None:
            await repo.release_reservation_hold(operator_id, reservation_id, "cancelled")
            return ChargingStartResult(status="not_ocpp", reservation_id=reservation_id, id_tag=id_tag)

        cp_id = station["ocpp_charge_point_id"]
        try:
            accepted = await remote_start_transaction(operator_id, cp_id, id_tag=id_tag)
        except ChargePointNotConnected:
            await repo.release_reservation_hold(operator_id, reservation_id, "cancelled")
            return ChargingStartResult(status="not_connected", reservation_id=reservation_id, id_tag=id_tag)

        if not accepted:
            await repo.release_reservation_hold(operator_id, reservation_id, "cancelled")
            return ChargingStartResult(status="rejected", reservation_id=reservation_id, id_tag=id_tag)

        return ChargingStartResult(status="ok", reservation_id=reservation_id, id_tag=id_tag)
    except BaseException:
        logger.exception(
            "🔥 Неочікуваний збій між hold і RemoteStart — звільняю резервацію #%s "
            "(operator_id=%s, station_id=%s, user_id=%s)",
            reservation_id, operator_id, station_id, user_id,
        )
        try:
            # shield: захищає САМЕ від ПОВТОРНОГО task.cancel() на цій же
            # задачі (напр. штатний shutdown, що скасовує все двічі) — тоді
            # плейн await тут теж кинув би CancelledError і release не
            # дочекався б підтвердження. При одиночному cancel плейн await
            # у except-блоці й без shield спокійно добігає (прапорець
            # скасування знімається після першого кидка) — перевірено на
            # Python 3.11 (той самий інтерпретатор, що в Dockerfile і CI).
            # shield лишається про запас на другий сценарій, а не тому, що
            # без нього release взагалі не виконався б.
            await asyncio.shield(repo.release_reservation_hold(operator_id, reservation_id, "cancelled"))
        except BaseException:
            # Ловиться рівно в сценарії повторного cancel: зовнішній await
            # на shield кидає CancelledError, а сама корутина release під
            # shield могла і впасти, і успішно дожити у фоні — звідси ми
            # цього напевно не знаємо. Первинну причину це логування
            # маскувати не повинно:
            logger.error(
                "🔥 Не дочекались підтвердження звільнення резервації #%s (operator_id=%s) — "
                "release міг упасти, а міг і дожити у фоні під shield; фінальна сітка — "
                "крон reconcile_charging_reservations.py",
                reservation_id, operator_id,
            )
        raise


async def stop_charging_session(operator_id: int, station_id: int) -> ChargingStopResult:
    """
    RemoteStopTransaction на активну OCPP-сесію станції. Жодного settle
    тут: фактичне списання спожитого й звільнення залишку резерву робить
    ВИКЛЮЧНО on_stop_transaction (app/api/ocpp_ws.py), коли станція сама
    відповість власним StopTransaction.req — ця функція лише посилає
    команду й повертає, чи станція її прийняла.
    """
    station = await repo.get_station(operator_id, station_id)
    if station is None or station.get("ocpp_charge_point_id") is None:
        return ChargingStopResult(status="unknown_station")

    session = await repo.get_active_ocpp_session(operator_id, station_id)
    if session is None:
        return ChargingStopResult(status="no_active_session")

    transaction_id = session["ocpp_transaction_id"]
    cp_id = station["ocpp_charge_point_id"]
    try:
        accepted = await remote_stop_transaction(operator_id, cp_id, transaction_id)
    except ChargePointNotConnected:
        return ChargingStopResult(status="not_connected", transaction_id=transaction_id)

    if not accepted:
        return ChargingStopResult(status="rejected", transaction_id=transaction_id)

    return ChargingStopResult(status="ok", transaction_id=transaction_id)
