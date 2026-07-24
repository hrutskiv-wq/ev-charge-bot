"""
Ручний CLI-вхід у резервацію+зарядку (Промпт 3c-i, модель A: kWh-баланс,
"резерв наперед -> заряд -> списання факту -> звільнення решти"). Тонка
обгортка над app/services/ocpp_charging.py — те саме сервісне ядро тепер
використовують і бот-команди /ocpp_start/ocpp_stop
(app/handlers/ocpp_admin.py).

ВАЖЛИВО: цей скрипт запускається ОКРЕМИМ процесом (docker compose exec) —
його _active_charge_points завжди порожній, тому RemoteStart звідси на
реальному проді ЗАВЖДИ впаде ChargePointNotConnected (лише живий
uvicorn-процес бачить активні OCPP-з'єднання станцій; саме тому й з'явились
бот-команди — вони працюють IN-PROCESS). Скрипт лишається корисним для
локальної розробки (симулятор + застосунок в одному процесі, той самий
event loop — той самий підхід, що в review_prompt3c-i.md) і як приклад
виклику сервісного шару без бота.

Використання:
    python start_charging_session.py <operator_id> <station_id> <user_id> <reserved_kwh>
    docker compose exec bot python start_charging_session.py 1 10 555 20.0
"""
import argparse
import asyncio
from decimal import Decimal, InvalidOperation

from app.database import connection
from app.services.ocpp_charging import ChargingStartResult, start_charging_session as _start_charging_session

_STATUS_MESSAGES = {
    "unknown_station": lambda r: "❌ Резервацію не створено: станція не належить оператору",
    "insufficient_balance": lambda r: "❌ Резервацію не створено: недостатньо балансу",
    "not_ocpp": lambda r: f"❌ Резервацію #{r.reservation_id} звільнено: станція не є OCPP-станцією "
                          f"(немає ocpp_charge_point_id)",
    "not_connected": lambda r: f"❌ Резервацію #{r.reservation_id} звільнено: станція зараз не підключена "
                               f"до цього процесу",
    "rejected": lambda r: f"❌ Резервацію #{r.reservation_id} звільнено: станція відхилила "
                          f"RemoteStartTransaction",
}


async def start_charging_session(operator_id: int, station_id: int, user_id: int, reserved_kwh: Decimal):
    """
    Друкує прогрес у консоль і повертає (reservation_id, id_tag) при успіху,
    або (None, None) — уся логіка в app.services.ocpp_charging, тут лише
    форматування результату під консоль (той самий контракт, що й раніше).
    """
    result: ChargingStartResult = await _start_charging_session(operator_id, station_id, user_id, reserved_kwh)

    if result.status == "ok":
        print(f"🔒 Резервація #{result.reservation_id} створена: {reserved_kwh} кВт·год утримано на балансі "
              f"водія {user_id} (id_tag={result.id_tag})")
        print(f"✅ RemoteStartTransaction прийнято — очікую StartTransaction.req "
              f"для активації резервації #{result.reservation_id}")
        return result.reservation_id, result.id_tag

    formatter = _STATUS_MESSAGES.get(result.status, lambda r: f"❌ Невідомий статус: {r.status}")
    print(formatter(result))
    return None, None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Резерв kWh-балансу + RemoteStart на OCPP-станції (Промпт 3c-i, модель A).",
    )
    parser.add_argument("operator_id", type=int)
    parser.add_argument("station_id", type=int)
    parser.add_argument("user_id", type=int)
    parser.add_argument("reserved_kwh", type=str, help="Скільки кВт·год зарезервувати, напр. 20.0")
    args = parser.parse_args()

    try:
        reserved_kwh = Decimal(args.reserved_kwh)
    except InvalidOperation:
        parser.error(f"reserved_kwh має бути числом, отримано: {args.reserved_kwh!r}")
    if reserved_kwh <= 0:
        parser.error("reserved_kwh має бути додатним")

    async def _run():
        await connection.init_postgres()
        try:
            await start_charging_session(args.operator_id, args.station_id, args.user_id, reserved_kwh)
        finally:
            await connection.close_postgres()

    asyncio.run(_run())
