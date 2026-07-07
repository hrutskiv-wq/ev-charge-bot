import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

# Імпортуємо потрібні об'єкти та функції з main.py
from main import bot, dp, find_three_nearest_stations, initialize_db, db_connection

load_dotenv()
OCM_KEY = os.getenv("OCM_KEY")

logging.basicConfig(level=logging.INFO)

# Механізм Lifespan для фонового запуска бота разом із сервером
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Ініціалізація ресурсів...")
    await initialize_db()  # Ініціалізуємо БД

    logging.info("🤖 Запуск Telegram-бота у фоновому режимі...")
    # Створюємо фонову задачу для aiogram, щоб вона не блокувала вебсервер
    polling_task = asyncio.create_task(dp.start_polling(bot))
    
    yield  # Тут сервер активний і приймає запити від PWA
    
    logging.info("🛑 Зупинка фонових задач та звільнення ресурсів...")
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        logging.info("Фоновий процес бота успішно вимкнено.")
    
    if db_connection:
        await db_connection.close()
        logging.info("З'єднання з базою даних закрито.")

app = FastAPI(title="eVolt UA API Server", lifespan=lifespan)

# Дозволяємо крос-доменні запити (CORS) для нашого PWA
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ендпоінт, який PWA викликатиме для отримання 3 станцій
@app.get("/api/stations")
async def get_stations(lat: float = Query(...), lon: float = Query(...)):
    stations = await find_three_nearest_stations(lat, lon)
    if not stations:
        return {"success": False, "stations": []}
    return {"success": True, "stations": stations}

# Ендпоінт, який буде віддавати інтерфейс нашого PWA додатка
@app.get("/pwa")
async def get_pwa_index():
    return FileResponse("public/index.html")

# Підключаємо статичні файли (маніфест, іконки, сервіс-воркер)
app.mount("/", StaticFiles(directory="public"), name="public")