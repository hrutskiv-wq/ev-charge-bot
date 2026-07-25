"""
Адмінські бот-команди для OCPP-резервації (Промпт 3c-i): /ocpp_start і
/ocpp_stop. Вхідна точка IN-PROCESS (на відміну від start_charging_
session.py, який запускається окремим процесом `docker compose exec` і
тому НІКОЛИ не бачить `_active_charge_points` живого uvicorn-процесу —
RemoteStart звідти завжди падає ChargePointNotConnected). Хендлери тут
працюють у ТОМУ САМОМУ event loop, що й OCPP WS-роут
(app/api/ocpp_ws.py), тому реєстр реально доступний — це і є вся причина
існування цього файлу.

Гейт — той самий, що й admin_activate_operator у operator_billing.py:
лише int(message.chat.id) == int(LOGS_CHAT_ID). Раніше тут була ще й вимога
message.chat.type == "private" — прибрано (рев'ю): LOGS_CHAT_ID на проді
підтверджено СУПЕРГРУПОЮ (-100…), тож "приватний чат" НІКОЛИ не виконався
б і обидві команди були б недосяжні звідти, де адмін реально працює. Поза
гейтом — тиха відмова (жодної відповіді), той самий анти-оракул принцип,
що й у OCPP WS-хендшейку й Monobank-вебхуках: чужий чат не має дізнатись
навіть про факт існування цих команд.

Обидві команди — суто Command(...)-фільтровані, без жодного вільнотекстового
FSM-кроку. Реєструються ПЕРЕД user_router (app/main.py) — той самий,
обов'язковий порядок, що й operator_billing_router (див. докладний
коментар на початку app/handlers/operator_billing.py про хендлер-приймач
"будь-який текст без '/' -> ШІ-чат"): технічно для суто-командних
хендлерів порядок відносно цього catch-all не критичний (catch-all явно
виключає текст, що починається з '/'), але дотримуємось конвенції
репозиторію свідомо, а не лише сподіваємось на це — регресія на порядок
роутерів перевіряється напряму через справжній aiogram Dispatcher у
test_ocpp_admin_router.py.
"""
import logging
from decimal import Decimal, InvalidOperation

from aiogram import Router
from aiogram.filters import Command, StateFilter
from aiogram.types import Message

from app.handlers.operator_billing import _is_from_admin_chat
from app.services import ocpp_charging

logger = logging.getLogger(__name__)

router = Router()


_START_STATUS_MESSAGES = {
    "unknown_station": "❌ Станція #{station_id} не належить оператору #{operator_id}",
    "insufficient_balance": "❌ Недостатньо kWh-балансу у водія {user_id}",
    "not_ocpp": "❌ Резервацію #{reservation_id} звільнено: станція #{station_id} не в режимі OCPP",
    "not_connected": "❌ Резервацію #{reservation_id} звільнено: станція зараз не підключена до цього процесу",
    "rejected": "❌ Резервацію #{reservation_id} звільнено: станція відхилила RemoteStartTransaction",
}

_STOP_STATUS_MESSAGES = {
    "unknown_station": "❌ Станція #{station_id} не належить оператору #{operator_id}",
    "no_active_session": "ℹ️ На станції #{station_id} немає активної OCPP-сесії",
    "not_connected": "❌ Станція зараз не підключена до цього процесу (transactionId={transaction_id})",
    "rejected": "❌ Станція відхилила RemoteStopTransaction (transactionId={transaction_id})",
}


def _is_admin_chat(message: Message) -> bool:
    return _is_from_admin_chat(message.chat.id)


@router.message(Command("ocpp_start"), StateFilter("*"))
async def cmd_ocpp_start(message: Message):
    if not _is_admin_chat(message):
        return

    args = (message.text or "").split()[1:]
    if len(args) != 4:
        await message.answer("Використання: /ocpp_start <operator_id> <station_id> <user_id> <reserved_kwh>")
        return

    try:
        operator_id = int(args[0])
        station_id = int(args[1])
        user_id = int(args[2])
        reserved_kwh = Decimal(args[3])
    except (ValueError, InvalidOperation):
        await message.answer(
            "❌ Биті аргументи: operator_id/station_id/user_id мають бути цілими числами, "
            "reserved_kwh — числом (напр. 20.0)",
        )
        return

    if reserved_kwh <= 0:
        await message.answer("❌ reserved_kwh має бути додатним")
        return

    logger.info("🔌 /ocpp_start від адмін-чату %s: operator=%s station=%s user=%s reserved_kwh=%s",
                message.chat.id, operator_id, station_id, user_id, reserved_kwh)

    try:
        result = await ocpp_charging.start_charging_session(operator_id, station_id, user_id, reserved_kwh)
    except Exception:
        # ocpp_charging.start_charging_session() НАМАГАЄТЬСЯ звільнити hold
        # (шлях try/except BaseException всередині сервісу) і перевикидає
        # оригінальний виняток — але це не гарантія: виняток міг статись і
        # в repo.create_charging_reservation() (резервації тоді взагалі не
        # існує, тож і звільняти нічого), і компенсуюче звільнення саме
        # могло впасти (тоді сервіс лише залогував і лишив hold висіти на
        # крон-звірку). Тому текст нижче НЕ стверджує напевно, що резерв
        # повернено — лише UX: адмін має побачити ЩОСЬ, інакше команда
        # мовчки не відповість. Текст самого винятку в чат не йде
        # (анти-оракул, той самий принцип, що в OCPP-хендшейку й
        # Monobank-вебхуку) — деталі лише в лог.
        logger.exception(
            "🔥 /ocpp_start: неочікуваний збій сервісу (operator=%s station=%s user=%s)",
            operator_id, station_id, user_id,
        )
        await message.answer(
            "⚠️ Внутрішня помилка. Резерв мав бути звільнений автоматично — "
            "ПЕРЕВІР лог і баланс водія. Якщо hold завис, його добере "
            "reconcile_charging_reservations.py."
        )
        return

    if result.status == "ok":
        await message.answer(
            f"✅ Резервація #{result.reservation_id} активна: {reserved_kwh} кВт·год утримано на балансі "
            f"водія {user_id}, RemoteStart прийнято станцією #{station_id}. Очікую StartTransaction.req.",
        )
        return

    template = _START_STATUS_MESSAGES.get(result.status, "❌ Невідомий статус: {status}")
    await message.answer(template.format(
        operator_id=operator_id, station_id=station_id, user_id=user_id,
        reservation_id=result.reservation_id, status=result.status,
    ))


@router.message(Command("ocpp_stop"), StateFilter("*"))
async def cmd_ocpp_stop(message: Message):
    if not _is_admin_chat(message):
        return

    args = (message.text or "").split()[1:]
    if len(args) != 2:
        await message.answer("Використання: /ocpp_stop <operator_id> <station_id>")
        return

    try:
        operator_id = int(args[0])
        station_id = int(args[1])
    except ValueError:
        await message.answer("❌ Биті аргументи: operator_id/station_id мають бути цілими числами")
        return

    logger.info("🔌 /ocpp_stop від адмін-чату %s: operator=%s station=%s",
                message.chat.id, operator_id, station_id)

    try:
        result = await ocpp_charging.stop_charging_session(operator_id, station_id)
    except Exception:
        # На відміну від /ocpp_start, тут гроші не рухаються (stop_charging_
        # session() ніякого hold/release не робить — settle робить виключно
        # on_stop_transaction на StopTransaction.req від станції), тож текст
        # інший: нічого "повертати" немає, лише повідомити про сам збій.
        logger.exception(
            "🔥 /ocpp_stop: неочікуваний збій сервісу (operator=%s station=%s)",
            operator_id, station_id,
        )
        await message.answer("⚠️ Внутрішня помилка. Перевір лог і спробуй ще раз.")
        return

    if result.status == "ok":
        await message.answer(
            f"✅ RemoteStopTransaction прийнято станцією #{station_id} "
            f"(transactionId={result.transaction_id}). Списання факту й звільнення залишку "
            f"відбудуться автоматично на StopTransaction.req від станції.",
        )
        return

    template = _STOP_STATUS_MESSAGES.get(result.status, "❌ Невідомий статус: {status}")
    await message.answer(template.format(
        operator_id=operator_id, station_id=station_id, transaction_id=result.transaction_id,
        status=result.status,
    ))
