import os
import logging
import json
import urllib.request
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from google import genai

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
            
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": f"⚠️ <b>Збій у системі eVolt UA!</b>\n<pre><code class='language-python'>{log_entry}</code></pre>",
                "parse_mode": "HTML"
            }
            
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass # Якщо впаде сам Telegram API, ігноруємо, щоб не зациклити логер

load_dotenv()

# 2. Налаштування глобальної системи логування
token = os.getenv("BOT_TOKEN")
chat_id = os.getenv("LOGS_CHAT_ID")

log_handlers = [logging.StreamHandler()] # Залишаємо стандартне логування в консоль Docker

if token and chat_id:
    tg_handler = TelegramLogsHandler(token, chat_id)
    tg_handler.setLevel(logging.ERROR) # Тільки помилки рівня ERROR та CRITICAL летять в чат
    log_handlers.append(tg_handler)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)

# 3. Ініціалізація компонентів бота
bot = Bot(token=token)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
