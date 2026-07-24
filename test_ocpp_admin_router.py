"""
Регресія на порядок роутерів (app/main.py): /ocpp_start має дійти до
cmd_ocpp_start (app/handlers/ocpp_admin.py), а НЕ до хендлера-приймача
"будь-який текст без '/' -> ШІ-чат" (app/handlers/user.py::handle_ai_chat).

На відміну від test_ocpp_admin_handlers.py (хендлер викликається напряму,
фільтри aiogram не задіяні), тут — СПРАВЖНІЙ aiogram Dispatcher зі
СПРАВЖНІМИ Command/StateFilter фільтрами й ТОЧНО той самий порядок
роутерів, що й у проді: import app.main (як у test_health.py) замість
збирання окремого Dispatcher() вручну — інакше тест міг би розійтися зі
справжньою реєстрацією й перестати щось насправді перевіряти. Це ж і
уникає подвійної прив'язки роутерів (aiogram Router можна прикріпити лише
до ОДНОГО Dispatcher — якщо інший тест-файл уже імпортував app.main,
роутери вже прикріплені до app.core.loader.dp; використовуємо ТОЙ САМИЙ
dp/bot, а не створюємо другий).

Мережі й БД немає: Bot.__call__ підмінено (message.answer() у aiogram 3.x
повертає SendMessage(...).as_(bot) — запит іде через Bot.__call__, НЕ через
окремий метод Bot.send_message, тому патчити треба саме __call__).
ai_client.models.generate_content підмінено (для /ocpp_start кидає, якщо
взагалі викликаний — це й довело б, що маршрутизація зламалась), сервісний
виклик ocpp_charging.start_charging_session/stop_charging_session
підмінено фейком.

Запуск: BOT_TOKEN=... GEMINI_API_KEY=... pytest test_ocpp_admin_router.py -v
"""
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.types import Update

import app.main as main_module
from app.handlers import user as user_module
from app.services import ocpp_charging

ADMIN_CHAT_ID = 900001


def _update(text, chat_id, chat_type):
    return Update.model_validate({
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": abs(chat_id), "is_bot": False, "first_name": "Тест"},
            "text": text,
        },
    })


def _patch_bot_call(monkeypatch):
    """
    Перехоплює УСІ вихідні виклики Telegram Bot API (SendMessage,
    SendChatAction тощо) в одному місці — вони всі йдуть через
    Bot.__call__, не через окремі send_message()/send_chat_action().
    Повертає список фактично виконаних TelegramMethod-обʼєктів.
    """
    calls = []

    async def fake_call(self, method, request_timeout=None):
        calls.append(method)
        return None

    monkeypatch.setattr(Bot, "__call__", fake_call)
    return calls


async def test_ocpp_start_command_reaches_its_own_handler_not_ai_chat(monkeypatch):
    monkeypatch.setenv("LOGS_CHAT_ID", str(ADMIN_CHAT_ID))
    sent = _patch_bot_call(monkeypatch)

    ai_chat_calls = []
    def fake_generate_content(*a, **kw):
        ai_chat_calls.append((a, kw))
        raise AssertionError("handle_ai_chat не мав викликатись для /ocpp_start")
    monkeypatch.setattr(user_module.ai_client.models, "generate_content", fake_generate_content)

    service_calls = []
    async def fake_service_start(operator_id, station_id, user_id, reserved_kwh):
        service_calls.append((operator_id, station_id, user_id, reserved_kwh))
        return ocpp_charging.ChargingStartResult(status="ok", reservation_id=1, id_tag="x")
    monkeypatch.setattr(ocpp_charging, "start_charging_session", fake_service_start)

    bot = main_module.bot
    dp = main_module.dp

    await dp.feed_update(bot, _update("/ocpp_start 1 10 555 20.0", ADMIN_CHAT_ID, "supergroup"))

    assert ai_chat_calls == [], "Команда впала в catch-all ШІ-чату — роутер зареєстровано у неправильному порядку"
    assert len(service_calls) == 1
    assert len(sent) == 1
    assert "✅" in sent[0].text


async def test_ocpp_stop_command_reaches_its_own_handler_not_ai_chat(monkeypatch):
    monkeypatch.setenv("LOGS_CHAT_ID", str(ADMIN_CHAT_ID))
    sent = _patch_bot_call(monkeypatch)

    ai_chat_calls = []
    def fake_generate_content(*a, **kw):
        ai_chat_calls.append((a, kw))
        raise AssertionError("handle_ai_chat не мав викликатись для /ocpp_stop")
    monkeypatch.setattr(user_module.ai_client.models, "generate_content", fake_generate_content)

    service_calls = []
    async def fake_service_stop(operator_id, station_id):
        service_calls.append((operator_id, station_id))
        return ocpp_charging.ChargingStopResult(status="ok", transaction_id=42)
    monkeypatch.setattr(ocpp_charging, "stop_charging_session", fake_service_stop)

    bot = main_module.bot
    dp = main_module.dp

    await dp.feed_update(bot, _update("/ocpp_stop 1 10", ADMIN_CHAT_ID, "supergroup"))

    assert ai_chat_calls == []
    assert len(service_calls) == 1
    assert len(sent) == 1


async def test_free_text_still_reaches_ai_chat_when_not_a_command(monkeypatch):
    """Негативний контроль: звичайний текст (не команда) і далі йде в catch-all — регресію не зламано в інший бік."""
    monkeypatch.delenv("LOGS_CHAT_ID", raising=False)
    _patch_bot_call(monkeypatch)

    ai_chat_calls = []
    def fake_generate_content(*a, **kw):
        ai_chat_calls.append((a, kw))
        class _Resp:
            text = "ок"
        return _Resp()
    monkeypatch.setattr(user_module.ai_client.models, "generate_content", fake_generate_content)

    bot = main_module.bot
    dp = main_module.dp

    await dp.feed_update(bot, _update("привіт, де найближча станція", 12345, "private"))

    assert len(ai_chat_calls) == 1
