import json
import logging
import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from app.database import connection
from app.database.connection import update_user_balance
from app.services.ocpi.config import OCPIConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ocpi/emsp/2.2.1", tags=["OCPI EMSP CDRs"])


async def verify_ocpi_token(authorization: str = Header(default=None)):
    """
    Перевіряє, що вхідний запит від CPO несе правильний OCPI-токен
    (Authorization: Token <OCPI_SECRET_TOKEN>). Без цієї перевірки будь-хто
    міг надіслати фейковий CDR або callback і списати/нарахувати кошти
    довільному користувачу.
    """
    expected = f"Token {OCPIConfig.OCPI_TOKEN}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        logger.warning("⛔ Відхилено OCPI-запит з невалідним або відсутнім заголовком Authorization.")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing OCPI token")


@router.post("/cdrs", dependencies=[Depends(verify_ocpi_token)])
async def receive_cdr(cdr: dict):
    """
    Приймає фінальний CDR від CPO.
    Фіксує його в БД та списує кВт·год з балансу водія.
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

    if total_energy < 0 or total_cost < 0:
        # CDR з від'ємними значеннями не може бути легітимним і раніше міг
        # використовуватись для накрутки балансу (withdrawal з від'ємною сумою
        # ставав поповненням). Відхиляємо одразу.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="total_energy та total_cost не можуть бути від'ємними."
        )

    if not connection.db_pool:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Пул підключень до PostgreSQL не ініціалізовано."
        )

    async with connection.db_pool.acquire() as conn:
        async with conn.transaction():
            # 1. Перевірка на дублікати (ідемпотентність по cdr_id)
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

            # 3. Списуємо кВт·год з балансу користувача.
            #    ВАЖЛИВО: баланс користувача (users.balance) ведеться в кВт·год,
            #    тому тут списується total_energy (кВт·год), а НЕ total_cost
            #    (грошова вартість сесії у валюті CPO) — раніше тут помилково
            #    віднімався total_cost, через що users.balance взагалі не
            #    оновлювався (списання йшло лише в журнал kw_transactions) і
            #    з часом розходився з реальним балансом.
            await update_user_balance(
                user_id=user_id,
                amount_kwh=total_energy,
                t_type="ocpi_session",
                conn=conn,
                session_id=session_id,
                description=f"Списання за зарядку. Сесія {session_id}. Спожито: {total_energy} кВт·год "
                             f"(вартість у CPO: {total_cost}).",
            )

    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }


# --- CALLBACK ENDPOINT FOR REMOTE COMMANDS (OCPI 2.2.1) ---

@router.post("/callback/commands/START_SESSION/{user_id}", dependencies=[Depends(verify_ocpi_token)])
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


@router.post("/callback/commands/STOP_SESSION/{user_id}", dependencies=[Depends(verify_ocpi_token)])
async def ocpi_stop_session_callback(user_id: int, request: Request):
    """
    Асинхронний Callback від CPO про статус фізичної зупинки сесії заряджання.
    """
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Помилка парсингу JSON у callback STOP_SESSION: {e}")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    result = payload.get("result")  # ACCEPTED, REJECTED, FAILED
    message = payload.get("message", "")

    logger.info(f"📡 Отримано callback STOP_SESSION для користувача {user_id}. Результат: {result}, Повідомлення: {message}")

    try:
        bot = request.app.state.bot
        if result == "ACCEPTED":
            text = (
                f"🏁 <b>Зарядку успішно завершено!</b>\n\n"
                f"🛑 Подачу струму припинено, кабель розблоковано.\n"
                f"Дякуємо, що користуєтесь eVolt UA! ⚡"
            )
        else:
            text = (
                f"⚠️ <b>Проблема при зупинці зарядки</b>\n\n"
                f"Станція повернула статус: <b>{result}</b>\n"
                f"Причина: {message or 'Не вдалося зупинити сесію автоматично.'}\n\n"
                f"Спробуйте зупинити сесію ще раз або зверніться до підтримки."
            )
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception as tg_err:
        logger.error(f"Не вдалося надіслати повідомлення про зупинку користувачу {user_id}: {tg_err}")

    return {
        "status_code": 1000,
        "status_message": "Success",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    }
