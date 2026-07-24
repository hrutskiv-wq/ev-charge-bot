"""
Ручний адмінський/CLI вхід у резервацію+зарядку (Промпт 3c-i, модель A:
kWh-баланс, "резерв наперед -> заряд -> списання факту -> звільнення
решти"). Без UI бота в цьому бандлі — той самий підхід, що
provision_ocpp_station.py для самого провіжну станції: реальний
telegram-хендлер прийде окремою задачею, коли модель A вже прожита живим
трафіком.

Композиція двох уже готових шматків:
  1. create_charging_reservation() (app/database/operators_repo.py) —
     атомарно резервує reserved_kwh на kWh-балансі водія й заводить
     'pending'-резервацію з новим id_tag.
  2. remote_start_transaction() (app/api/ocpp_ws.py) — Central System ->
     станція, RemoteStartTransaction.req зі щойно виданим id_tag; сама
     сесія відкриється, коли станція відповість власним StartTransaction.req
     (on_start_transaction), тоді on_start_transaction і привʼяже сесію до
     цієї резервації (_try_activate_reservation).

Якщо RemoteStart не підтвердився (станція відхилила / не підключена до
ЦЬОГО процесу — обмеження _active_charge_points, див. app/api/ocpp_ws.py) —
hold одразу звільняється назад через release_reservation_hold(), щоб гроші
водія не зависали без жодного шансу на зарядку.

Використання:
    python start_charging_session.py <operator_id> <station_id> <user_id> <reserved_kwh>
    docker compose exec bot python start_charging_session.py 1 10 555 20.0

Станція має бути OCPP (mode='ocpp') і зараз підключена до ЦЬОГО процесу
(живий WS у _active_charge_points) — інакше ChargePointNotConnected.
"""
import argparse
import asyncio
from decimal import Decimal, InvalidOperation

from app.api.ocpp_ws import ChargePointNotConnected, remote_start_transaction
from app.database import connection
from app.database import operators_repo as repo


async def start_charging_session(operator_id: int, station_id: int, user_id: int, reserved_kwh: Decimal):
    """
    Повертає (reservation_id, id_tag) при успіху, або (None, None) — у
    консоль вже надруковано причину відмови, звідси нічого далі виправляти.
    """
    reservation_id, id_tag, error = await repo.create_charging_reservation(
        operator_id, station_id, user_id, reserved_kwh,
    )
    if error is not None:
        print(f"❌ Резервацію не створено: {error}")
        return None, None

    print(f"🔒 Резервація #{reservation_id} створена: {reserved_kwh} кВт·год утримано на балансі "
          f"водія {user_id} (id_tag={id_tag})")

    station = await repo.get_station(operator_id, station_id)
    if station is None or station.get("ocpp_charge_point_id") is None:
        print(f"❌ Станція #{station_id} не є OCPP-станцією (немає ocpp_charge_point_id) — "
              f"звільняю резерв назад")
        await repo.release_reservation_hold(operator_id, reservation_id, "cancelled")
        return None, None

    cp_id = station["ocpp_charge_point_id"]
    try:
        accepted = await remote_start_transaction(operator_id, cp_id, id_tag=id_tag)
    except ChargePointNotConnected:
        print(f"❌ Станція {cp_id} зараз не підключена до цього процесу — звільняю резерв назад")
        await repo.release_reservation_hold(operator_id, reservation_id, "cancelled")
        return None, None

    if not accepted:
        print(f"❌ Станція {cp_id} відхилила RemoteStartTransaction — звільняю резерв назад")
        await repo.release_reservation_hold(operator_id, reservation_id, "cancelled")
        return None, None

    print(f"✅ RemoteStartTransaction прийнято станцією {cp_id} — очікую StartTransaction.req "
          f"для активації резервації #{reservation_id}")
    return reservation_id, id_tag


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
