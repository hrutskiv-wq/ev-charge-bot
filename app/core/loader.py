import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage # Імпортуємо пам'ять
from google import genai

load_dotenv()

# Ініціалізація
bot = Bot(token=os.getenv("BOT_TOKEN"))

# Використовуємо MemoryStorage замість Redis (це простіше для розробки)
storage = MemoryStorage() 
dp = Dispatcher(storage=storage)

# Ініціалізація AI
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))