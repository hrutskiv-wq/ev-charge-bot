import os
import html
import logging
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from google import genai

# Єдиний спільний виконавець для фонової відправки логів у Telegram, щоб
# синхронний urllib-виклик всередині emit() не блокував asyncio event loop
# (logging.Handler.emit завжди синхронний і міг раніше виконуватись прямо в
# корутині, що впала з помилкою).
_log_sender_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg-log-sender")


# 1. Створюємо кастомний Handler для перехоплення помилок
class TelegramLogsHandler(logging.Handler):
    def __init__(self, token, chat_id):
        super().__init__()
        self.token = token
        self.chat_id = chat_id

    def emit(self, record):
        try:
            log_entry = self.format(record)
            # Обрізаємо лог, якщо він довший за ліміт повідомлення Telegram
            if len(log_entry) > 4000:
                log_entry = log_entry[:4000] + "\n... [Текст логу обрізано]"

            # ВАЖЛИВО: екранування HTML-спецсимволів. Traceback'и практично
            # завжди містять '<', '>' або '&' (наприклад, "<coroutine object
            # ... at 0x...>"), а Telegram з parse_mode=HTML відхиляє такі
            # повідомлення як невалідний HTML. Через `except: pass` нижче ця
            # помилка раніше глушилась мовчки — сповіщення про критичні збої
            # регулярно губились саме тоді, коли вони найпотрібніші.
            safe_log_entry = html.escape(log_entry)

            payload = {
                "chat_id": self.chat_id,
                "text": f"⚠️ <b>Збій у системі eVolt UA!</b>\n<pre><code class='language-python'>{safe_log_entry}</code></pre>",
                "parse_mode": "HTML"
            }
            # Відправка винесена у фоновий тред-пул, щоб не блокувати
            # event loop синхронним мережевим викликом.
            _log_sender_executor.submit(self._send, payload)
        except Exception:
            pass  # Помилка форматування логу не повинна валити сам логер

    def _send(self, payload):
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # Якщо впаде сам Telegram API, ігноруємо, щоб не зациклити логер


load_dotenv()

# 2. Налаштування глобальної системи логування
token = os.getenv("BOT_TOKEN")
chat_id = os.getenv("LOGS_CHAT_ID")

log_handlers = [logging.StreamHandler()]  # Залишаємо стандартне логування в консоль Docker

if token and chat_id:
    tg_handler = TelegramLogsHandler(token, chat_id)
    tg_handler.setLevel(logging.ERROR)  # Тільки помилки рівня ERROR та CRITICAL летять в чат
    log_handlers.append(tg_handler)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)

# 3. Ініціалізація компонентів бота.
#    ЄДИНИЙ екземпляр Bot/Dispatcher для всього застосунку. Раніше окремі
#    Bot(token=...)/Dispatcher() створювались тут, у app/main.py і в
#    server.py — три клієнти з одним і тим самим токеном, що при
#    одночасному запуску кількох процесів провокувало 409 Conflict від
#    Telegram API на getUpdates. Тепер app/main.py імпортує bot і dp
#    звідси, а server.py — це лише сумісний шім навколо app.main:app.
bot = Bot(token=token)

# FSM-стан (наприклад, "чекаємо номер станції від користувача") раніше жив
# лише в MemoryStorage — тобто в оперативній пам'яті самого процесу. Redis
# уже був піднятий у docker-compose.yml (сервіс `redis`, REDIS_HOST=redis
# прокинутий у env бота), але жодного разу не використовувався — і кожен
# `docker compose restart`/деплой миттєво "забував", на якому кроці діалогу
# перебував користувач (aiogram просто повертав його в початковий стан).
# REDIS_HOST заданий -> RedisStorage (стан переживає рестарт контейнера).
# REDIS_HOST не заданий (напр. локальний запуск без Docker) -> MemoryStorage,
# щоб не вимагати обов'язкового Redis для розробки.
redis_host = os.getenv("REDIS_HOST")
if redis_host:
    redis_port = os.getenv("REDIS_PORT", "6379")
    storage = RedisStorage.from_url(f"redis://{redis_host}:{redis_port}/0")
    logging.info(f"🔌 FSM-стан бота зберігається в Redis ({redis_host}:{redis_port}).")
else:
    storage = MemoryStorage()
    logging.warning(
        "⚠️ REDIS_HOST не заданий — FSM-стан бота живе лише в пам'яті процесу "
        "і губиться при кожному рестарті контейнера. Задайте REDIS_HOST=redis "
        "у .env для персистентного стану."
    )

dp = Dispatcher(storage=storage)
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
