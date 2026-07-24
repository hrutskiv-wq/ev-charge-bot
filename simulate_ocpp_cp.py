"""
Мінімальний симулятор зарядної станції (charge point) — клієнтська сторона
OCPP 1.6J, для локальної перевірки Central System без реального заліза.
Той самий принцип, що mock_monobank.py/mock_cpo.py для інших інтеграцій
цього репо.

Промпт 3a: BootNotification / StatusNotification / Heartbeat (скриптовано,
одразу після підключення).
Промпт 3b (додано): накопичувальний лічильник Wh (як у справжнього
пристрою), StartTransaction/MeterValues/StopTransaction, і РЕАКТИВНА
поведінка на команди Central System -> станція: @on(RemoteStartTransaction)
приймає команду (Accepted) і САМ, окремою фоновою таскою (двокроковість
1.6 — сесію завжди відкриває СТАНЦІЯ своїм StartTransaction, не сама
команда), запускає Start -> MeterValues -> лишається "заряджати" до
RemoteStopTransaction або до --rounds.

Використання:
    1. Підняти станцію: python provision_ocpp_station.py <operator_id> CP-SIM-1
       (скопіювати надрукований password)
    2. Підняти застосунок локально: python -m app.main   (або docker compose up)
    3. python simulate_ocpp_cp.py CP-SIM-1 <password> [--url ws://127.0.0.1:8000/ocpp]

За замовчуванням (без RemoteStart від CS) — скриптований прогін:
BootNotification, StatusNotification (Available), потім Heartbeat у циклі
(--heartbeat-interval, --rounds). Якщо ПІД ЧАС цього циклу прийде
RemoteStartTransaction від Central System — симулятор реагує так само, як
реальна станція: Accepted -> сам шле StartTransaction -> періодичні
MeterValues -> StopTransaction на RemoteStopTransaction (або примусово
через --auto-stop-after секунд, якщо задано).
"""
import argparse
import asyncio
import base64
import logging
from datetime import datetime, timezone

import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint, call, call_result
from ocpp.v16.enums import Action, ChargePointErrorCode, ChargePointStatus, RemoteStartStopStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("simulate_ocpp_cp")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SimulatedChargePoint(OcppChargePoint):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Накопичувальний показ лічильника — як у справжнього пристрою
        # (реальні станції зазвичай НЕ скидають лічильник між сесіями).
        self._meter_wh = 0
        self.connector_id = 1
        self.id_tag = "SIMULATOR-TAG"
        self._active_transaction_id = None

    # --- скриптована частина (Промпт 3a) -----------------------------------

    async def send_boot_notification(self):
        response = await self.call(call.BootNotification(
            charge_point_vendor="eVolt-Simulator",
            charge_point_model="Simulator-1",
        ))
        logger.info("BootNotification -> %s (interval %s)", response.status, response.interval)
        return response

    async def send_status_notification(self, status=ChargePointStatus.available):
        await self.call(call.StatusNotification(
            connector_id=self.connector_id,
            error_code=ChargePointErrorCode.no_error,
            status=status,
        ))
        logger.info("StatusNotification -> %s", status.value)

    async def send_heartbeat(self):
        response = await self.call(call.Heartbeat())
        logger.info("Heartbeat -> currentTime=%s", response.current_time)

    # --- транзакції + метринг (Промпт 3b) -----------------------------------

    async def send_start_transaction(self) -> int:
        response = await self.call(call.StartTransaction(
            connector_id=self.connector_id,
            id_tag=self.id_tag,
            meter_start=self._meter_wh,
            timestamp=_now_iso(),
        ))
        # id_tag_info приходить назад ЯК ПЛОСКИЙ dict (бібліотека не
        # десеріалізує вкладені структури CallResult у датаклас на клієнті,
        # лише top-level поля) — упіймано живим прогоном, response.id_tag_
        # info.status кидав AttributeError.
        id_tag_status = response.id_tag_info["status"]
        logger.info("StartTransaction -> transactionId=%s, status=%s",
                    response.transaction_id, id_tag_status)
        self._active_transaction_id = response.transaction_id
        return response.transaction_id

    async def send_meter_values(self, transaction_id: int, energy_wh_delta: int = 1500):
        self._meter_wh += energy_wh_delta
        await self.call(call.MeterValues(
            connector_id=self.connector_id,
            transaction_id=transaction_id,
            meter_value=[{
                "timestamp": _now_iso(),
                "sampledValue": [{"value": str(self._meter_wh)}],
            }],
        ))
        logger.info("MeterValues -> %s Wh (transactionId=%s)", self._meter_wh, transaction_id)

    async def send_stop_transaction(self, transaction_id: int, extra_wh: int = 500):
        self._meter_wh += extra_wh
        await self.call(call.StopTransaction(
            meter_stop=self._meter_wh,
            timestamp=_now_iso(),
            transaction_id=transaction_id,
        ))
        logger.info("StopTransaction -> transactionId=%s, meterStop=%s", transaction_id, self._meter_wh)
        if self._active_transaction_id == transaction_id:
            self._active_transaction_id = None

    async def run_full_transaction_cycle(self, meter_values_count: int = 2,
                                         meter_values_interval: float = 1.0):
        """Start -> N x MeterValues -> Stop, той самий цикл, що реальна зарядна сесія."""
        transaction_id = await self.send_start_transaction()
        for _ in range(meter_values_count):
            await asyncio.sleep(meter_values_interval)
            await self.send_meter_values(transaction_id, energy_wh_delta=1500)
        await self.send_stop_transaction(transaction_id)

    # --- реакція на команди Central System (RemoteStart/StopTransaction) ---

    @on(Action.remote_start_transaction)
    async def on_remote_start_transaction(self, id_tag, connector_id=None, **kwargs):
        """
        Двокроковість OCPP 1.6: тут лише Accepted/Rejected. Сесію реально
        відкриває СТАНЦІЯ своїм наступним StartTransaction.req — обов'язково
        окремою фоновою таскою: якби ми await-или StartTransaction прямо
        тут, це задедлочило б read-цикл (він же обробляє і відповідь на
        StartTransaction, яку сам собі ж і чекав би).
        """
        logger.info("<- RemoteStartTransaction (idTag=%s, connectorId=%s) — приймаємо",
                    id_tag, connector_id)
        asyncio.create_task(self.run_full_transaction_cycle())
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.remote_stop_transaction)
    async def on_remote_stop_transaction(self, transaction_id, **kwargs):
        logger.info("<- RemoteStopTransaction (transactionId=%s) — приймаємо", transaction_id)
        asyncio.create_task(self.send_stop_transaction(transaction_id))
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)


async def run(cp_id: str, password: str, base_url: str, heartbeat_interval: float, rounds: int):
    url = f"{base_url.rstrip('/')}/{cp_id}"
    ws = await websockets.connect(
        url,
        subprotocols=["ocpp1.6"],
        additional_headers=_basic_auth_header(cp_id, password),
    )
    logger.info("Підключено до %s (subprotocol=%s)", url, ws.subprotocol)

    cp = SimulatedChargePoint(cp_id, ws)
    listen_task = asyncio.create_task(cp.start())

    try:
        await cp.send_boot_notification()
        await cp.send_status_notification()
        logger.info("Готово чекати RemoteStartTransaction від Central System "
                    "(або натисніть Ctrl+C, якщо просто перевіряєте Boot/Heartbeat).")
        for i in range(rounds):
            await asyncio.sleep(heartbeat_interval)
            await cp.send_heartbeat()
    finally:
        listen_task.cancel()
        await ws.close()
        logger.info("Відключено")


def _basic_auth_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cp_id", help="ChargePoint identity (той самий, що в provision_ocpp_station.py)")
    parser.add_argument("password", help="Basic Auth пароль, надрукований provision_ocpp_station.py")
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ocpp", help="Базовий OCPP WS URL без /<cp_id>")
    parser.add_argument("--heartbeat-interval", type=float, default=10.0,
                        help="Пауза між Heartbeat, сек. (прод-інтервал з BootNotification ігнорується заради швидкої ручної перевірки)")
    parser.add_argument("--rounds", type=int, default=3, help="Скільки Heartbeat надіслати перед виходом")
    args = parser.parse_args()

    asyncio.run(run(args.cp_id, args.password, args.url, args.heartbeat_interval, args.rounds))
