import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, Response, status
from app.database import connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ocpi/emsp/2.2.1", tags=["OCPI EMSP CDRs"])

@router.post("/cdrs")
async def receive_cdr(cdr: dict):
    """
    Приймає фінальний CDR від CPO.
    Фіксує його в БД та списує гроші з балансу водія.
    """
    try:
        cdr_id = cdr.get("id")
        session_id = cdr.get("session_id")
        total_energy = float(cdr.get("total_energy", 0.0))
        total_cost = float(cdr.get("total_cost", 0.0))
        user_id = int(cdr.get("auth_id", 0))
    except (ValueError, TypeError, AttributeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail=f"Некоректний формат даних CDR: {str(e)}"
        )

    if not cdr_id or not session_id or not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Відсутні обов'язкові поля: id, session_id або auth_id."
        )

    if not connection.db_pool:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Пул підключень до PostgreSQL не ініціалізовано."
        )

    async with connection.db_pool.acquire() as conn:
        async with conn.transaction():
            # 1. Перевірка на дублікати
            exists = await conn.fetchval("SELECT id FROM ocpi_cdrs WHERE cdr_id = $1", cdr_id)
            if exists:
                return {
                    "status_code": 1000,
                    "status_message": "CDR already processed",
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                }

            # 2. Зберігаємо CDR
            await conn.execute("""
                INSERT INTO ocpi_cdrs (cdr_id, user_id, session_id, total_energy, total_cost, raw_payload)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, cdr_id, user_id, session_id, total_energy, total_cost, json.dumps(cdr))

            # 3. Списуємо кошти (withdrawal - від'ємне значення)
            await conn.execute("""
                INSERT INTO kw_transactions (user_id, type, amount, session_id, description)
                VALUES ($1, 'withdrawal', $2, $3, $4)
            """, 
                user_id, 
                -total_cost, 
                session_id, 
                f"Списання за зарядку. Сесія {session_id}. Спожито: {total_energy} кВт-год."
            )

    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }


# --- CALLBACK ENDPOINT FOR REMOTE COMMANDS (OCPI 2.2.1) ---

@router.post("/callback/commands/START_SESSION/{user_id}")
async def ocpi_start_session_callback(user_id: int, request: Request):
    """
    Асинхронний Callback від CPO про статус фізичного запуску сесії заряджання.
    """
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Помилка парсингу JSON у callback: {e}")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    result = payload.get("result")  # ACCEPTED, REJECTED, FAILED
    message = payload.get("message", "")
    
    logger.info(f"📡 Отримано callback START_SESSION для користувача {user_id}. Результат: {result}, Повідомлення: {message}")

    try:
        bot = request.app.state.bot
        if result == "ACCEPTED":
            text = (
                f"🔌 <b>Зарядку успішно активовано!</b>\n\n"
                f"🔋 Станція підтвердила фізичний старт сесії.\n"
                f"Приємної зарядки з eVolt UA! ⚡"
            )
        else:
            text = (
                f"❌ <b>Помилка фізичного запуску зарядки</b>\n\n"
                f"Станція повернула статус: <b>{result}</b>\n"
                f"Причина: {message or 'Фізична помилка підключення кабелю.'}\n\n"
                f"Будь ласка, перевірте з'єднання з електромобілем та спробуйте ще раз."
            )
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception as tg_err:
        logger.error(f"Не вдалося надіслати повідомлення користувачу {user_id}: {tg_err}")

    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    }
