"""
Мінімальний симулятор зарядної станції (charge point) — клієнтська сторона
OCPP 1.6J, для локальної перевірки Central System (Промпт 3a,
app/api/ocpp_ws.py) без реального заліза. Той самий принцип, що
mock_monobank.py/mock_cpo.py для інших інтеграцій цього репо.

Використання:
    1. Підняти станцію: python provision_ocpp_station.py <operator_id> CP-SIM-1
       (скопіювати надрукований password)
    2. Підняти застосунок локально: python -m app.main   (або docker compose up)
    3. python simulate_ocpp_cp.py CP-SIM-1 <password> [--url ws://127.0.0.1:8000/ocpp]

Надсилає BootNotification, потім StatusNotification (Available), потім
Heartbeat у циклі з інтервалом, який Central System повернула у відповіді
на BootNotification (типово 300с — за замовчуванням тут прискорено до 10с
через --heartbeat-interval для зручності ручної перевірки).
"""
import argparse
import asyncio
import logging

import websockets
from ocpp.v16 import ChargePoint as OcppChargePoint, call
from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("simulate_ocpp_cp")


class SimulatedChargePoint(OcppChargePoint):
    async def send_boot_notification(self):
        response = await self.call(call.BootNotification(
            charge_point_vendor="eVolt-Simulator",
            charge_point_model="Simulator-1",
        ))
        logger.info("BootNotification -> %s (interval %s)", response.status, response.interval)
        return response

    async def send_status_notification(self, status=ChargePointStatus.available):
        await self.call(call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=status,
        ))
        logger.info("StatusNotification -> %s", status.value)

    async def send_heartbeat(self):
        response = await self.call(call.Heartbeat())
        logger.info("Heartbeat -> currentTime=%s", response.current_time)


async def run(cp_id: str, password: str, base_url: str, heartbeat_interval: float, rounds: int):
    url = f"{base_url.rstrip('/')}/{cp_id}"
    headers = {}
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
        for i in range(rounds):
            await asyncio.sleep(heartbeat_interval)
            await cp.send_heartbeat()
    finally:
        listen_task.cancel()
        await ws.close()
        logger.info("Відключено")


def _basic_auth_header(username: str, password: str) -> dict:
    import base64
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
