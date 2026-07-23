"""
Ручний адмінський скрипт: створює (або оновлює) OCPP-станцію (mode='ocpp')
і видає для неї спільний ключ Basic Auth (OCPP security profile 1) —
потрібен, щоб локально прогнати симулятор проти Central System (Промпт 3a,
app/api/ocpp_ws.py). Майстер станції в Telegram-кабінеті поки що OCPP-поля
не запитує (окрема задача поза 3a) — тому провіжн станції для тестів робить
цей скрипт, той самий підхід, що й inject_deposit.py/refund_transaction.py
для балансу.

Використання:
    python provision_ocpp_station.py <operator_id> <charge_point_id> \\
        [--name "Тестова OCPP-станція"] [--tariff 10] [--station-id N]

Без --station-id створює НОВУ станцію (mode='ocpp', тариф --tariff,
за замовчуванням 10 грн/кВт·год) і одразу видає їй auth-ключ. З
--station-id лише перевидає ключ уже наявній станції оператора (напр. якщо
ключ загубили або хочуть ротувати).

Пароль друкується у ВІДКРИТОМУ вигляді ОДИН раз, у консоль — скопіювати в
конфіг симулятора одразу. У БД лишається лише Fernet-шифрована версія
(ENCRYPTION_KEY, той самий ключ, що й для operators.monobank_token_
encrypted) — повторно прочитати пароль зі скрипта чи БД неможливо.
"""
import argparse
import asyncio
import secrets

from app.core.crypto import encrypt_secret
from app.database import connection
from app.database import operators_repo as repo


async def provision(operator_id: int, charge_point_id: str, name: str,
                    tariff_uah_kwh: float, station_id: int = None):
    await connection.init_postgres()
    try:
        if station_id is None:
            station_id, qr_slug = await repo.create_station(
                operator_id, name, tariff_uah_kwh,
                mode="ocpp", ocpp_charge_point_id=charge_point_id,
            )
            print(f"✅ Створено станцію #{station_id} (оператор {operator_id}, qr_slug={qr_slug})")

        auth_key = secrets.token_urlsafe(24)
        updated = await repo.set_station_ocpp_auth_key(
            operator_id, station_id, encrypt_secret(auth_key),
        )
        if not updated:
            print(f"❌ Станція #{station_id} не належить оператору {operator_id} — перевірте id")
            return

        print("🔑 OCPP облікові дані для симулятора (скопіюйте зараз — вдруге не покажуться):")
        print(f"   URL:                  ws://<host>/ocpp/{charge_point_id}")
        print(f"   Sec-WebSocket-Protocol: ocpp1.6")
        print(f"   Basic Auth username:  {charge_point_id}")
        print(f"   Basic Auth password:  {auth_key}")
    finally:
        await connection.close_postgres()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Провіжн OCPP-станції для локального тесту Промпту 3a.")
    parser.add_argument("operator_id", type=int)
    parser.add_argument("charge_point_id", help="ChargePoint identity — сегмент URL /ocpp/<...>")
    parser.add_argument("--name", default="Тестова OCPP-станція")
    parser.add_argument("--tariff", type=float, default=10.0, help="tariff_uah_kwh (лише для нової станції)")
    parser.add_argument("--station-id", type=int, default=None,
                        help="Якщо задано — не створює нову станцію, лише перевидає ключ наявній")
    args = parser.parse_args()

    asyncio.run(provision(args.operator_id, args.charge_point_id, args.name,
                          args.tariff, args.station_id))
