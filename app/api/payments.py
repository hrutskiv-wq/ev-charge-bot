import base64
import logging
import json
import os

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import APIRouter, Request, Response, status

# Імпортуємо централізоване підключення та тариф
from app.database import connection
from app.database.connection import PRICE_PER_KWH, update_user_balance

logger = logging.getLogger(__name__)

# Створюємо роутер для FastAPI
payments_router = APIRouter()

# Токен мерчанта Monobank (X-Token), потрібен лише для одноразового запиту
# публічного ключа на GET /api/merchant/pubkey. Це НЕ те саме, що
# PAYMENT_PROVIDER_TOKEN (токен провайдера платежів Telegram) — навмисно
# окрема змінна середовища.
MONOBANK_API_TOKEN = os.getenv("MONOBANK_API_TOKEN")
MONOBANK_PUBKEY_URL = "https://api.monobank.ua/api/merchant/pubkey"

# Кеш публічного ключа в пам'яті процесу (per Monobank docs — не варто
# запитувати ключ на кожен webhook, лише коли перевірка перестала проходити).
_cached_pubkey = None


async def _fetch_monobank_pubkey(force_refresh: bool = False):
    global _cached_pubkey
    if _cached_pubkey is not None and not force_refresh:
        return _cached_pubkey

    if not MONOBANK_API_TOKEN:
        raise RuntimeError(
            "MONOBANK_API_TOKEN не заданий у середовищі — неможливо отримати "
            "публічний ключ для верифікації підпису webhook від Monobank."
        )

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            MONOBANK_PUBKEY_URL,
            headers={"X-Token": MONOBANK_API_TOKEN},
            timeout=10.0,
        )
        resp.raise_for_status()
        key_b64 = resp.json()["key"]

    pem_bytes = base64.b64decode(key_b64)
    _cached_pubkey = load_pem_public_key(pem_bytes)
    return _cached_pubkey


async def _verify_monobank_signature(raw_body: bytes, x_sign_header: str) -> bool:
    """
    Перевіряє підпис вебхука Monobank за схемою ECDSA/SHA-256 над сирим тілом
    запиту, як описано в офіційній документації:
    https://monobank.ua/api-docs/acquiring/dev/webhooks/verify

    Без цієї перевірки будь-хто, хто знає URL вебхука, міг надіслати
    підроблену "успішну оплату" з довільним Telegram ID у полі comment і
    безкоштовно нарахувати собі кВт·год.
    """
    if not x_sign_header:
        return False

    try:
        signature = base64.b64decode(x_sign_header)
    except Exception:
        return False

    # ec.ECDSA(hashes.SHA256()) сам хешує raw_body алгоритмом SHA-256 і
    # звіряє ASN.1/DER-підпис — так само, як ecdsa.VerifyASN1(...) у
    # прикладі Monobank на Go чи crypto.createVerify('SHA256') у Node.
    try:
        pubkey = await _fetch_monobank_pubkey()
        pubkey.verify(signature, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        pass
    except Exception as e:
        logger.error(f"Помилка отримання/використання публічного ключа Monobank: {e}")
        return False

    # Якщо перевірка не пройшла з кешованим ключем — Monobank рекомендує
    # оновити ключ один раз і повторити спробу (ключ міг ротуватися).
    try:
        pubkey = await _fetch_monobank_pubkey(force_refresh=True)
        pubkey.verify(signature, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


# Підтримуємо обидва варіанти виклику ендпоінту
@payments_router.post("/webhook/monobank")
@payments_router.post("/webhook/mono")
async def monobank_webhook(request: Request):
    """
    Ендпоінт (Webhook), куди сервери Monobank будуть миттєво
    надсилати інформацію про кожну гривню, що влетіла в Банку.
    """
    raw_body = await request.body()

    x_sign = request.headers.get("x-sign")
    if not await _verify_monobank_signature(raw_body, x_sign):
        logger.warning("⛔ Відхилено webhook Monobank з невалідним або відсутнім підписом x-sign.")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = json.loads(raw_body)
    except Exception as e:
        logger.error(f"Помилка парсингу JSON від Monobank: {e}")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # Перевіряємо, чи це тип події - новий запис у виписці (StatementItem)
    if payload.get("type") == "StatementItem":
        data = payload.get("data", {})
        item = data.get("statementItem", {})

        # 1. Отримуємо унікальний ID транзакції від Монобанку для захисту від дублікатів
        transaction_id = item.get("id")
        if not transaction_id:
            logger.error("Monobank Webhook: у виписці відсутній унікальний id транзакції")
            return Response(status_code=status.HTTP_200_OK)

        # Монобанк присилає суму в копійках. Порівнюємо пакети по копійках
        # (int), а не по гривнях (float) — рівність float ненадійна.
        raw_amount = item.get("amount", 0)
        amount_uah = raw_amount / 100

        # Якщо сума мінусова або нуль — це списання (ігноруємо)
        if raw_amount <= 0:
            return Response(status_code=status.HTTP_200_OK)

        # Дістаємо коментар до платежу, куди мы зашили Telegram ID водія
        comment_raw = item.get("comment", "").strip()

        # Перевіряємо, чи в коментарі дійсно лежить числовий Telegram ID
        if comment_raw.isdigit():
            user_id = int(comment_raw)

            # Нараховуємо пакетні кВт·год відповідно до сплаченої суми
            # (порівняння по копійках — 75000 коп. = 750 грн, 135000 = 1350 грн)
            if raw_amount == 75000:
                kwh_to_add = 50.0
            elif raw_amount == 135000:
                kwh_to_add = 100.0
            else:
                # На випадок, якщо водій скинув іншу суму вручну (пропорційно тарифу)
                kwh_to_add = round(amount_uah / PRICE_PER_KWH, 2)

            # --- АТОМАРНИЙ ЗАПИС У ПОСТГРЕС ---
            try:
                async with connection.db_pool.acquire() as conn:
                    async with conn.transaction():

                        # Перевіряємо, чи цей платіж вже оброблявся раніше
                        existing_payment = await conn.fetchrow(
                            "SELECT id FROM payments WHERE invoice_id = $1",
                            transaction_id
                        )

                        if existing_payment:
                            logger.info(f"Транзакція Monobank {transaction_id} вже була успішно оброблена раніше. Пропускаємо.")
                            return Response(status_code=status.HTTP_200_OK)

                        # Оскільки це Моно-Банка, платіж фіксується одразу як успішний ('success')
                        payment_id = await conn.fetchval(
                            """
                            INSERT INTO payments (user_id, invoice_id, amount, provider, status, payload, created_at, updated_at)
                            VALUES ($1, $2, $3, 'monobank', 'success', $4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            RETURNING id
                            """,
                            user_id, transaction_id, amount_uah, json.dumps(payload)
                        )

                        # Нараховуємо кВт·год. Раніше тут писався прямий INSERT в
                        # kw_transactions, який НЕ оновлював users.balance — бот
                        # показував старий баланс, поки не оновиться з інших джерел.
                        # Тепер усе йде через update_user_balance(), яка тримає
                        # users.balance і журнал в одній транзакції.
                        description = f"Успішна оплата пакету: {kwh_to_add} кВт·год через Банку Monobank"
                        await update_user_balance(
                            user_id=user_id,
                            amount_kwh=kwh_to_add,
                            t_type="deposit",
                            conn=conn,
                            payment_id=payment_id,
                            description=description,
                        )

                logger.info(f"Успішно фінансово зараховано {kwh_to_add} кВт·год для користувача {user_id} через Банку Моно")

            except Exception as db_err:
                logger.error(f"Критична помилка транзакції бази даних для платежу {transaction_id}: {db_err}")
                return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # --- ВІДПРАВКА МИТТЄВОГО СПОВІЩЕННЯ В ТЕЛЕГРАМ ---
            try:
                bot = request.app.state.bot
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🎉 <b>Тарифний пакет активовано!</b>\n\n"
                        f"💳 Отримано платіж через Monobank: <b>{amount_uah:.2f} грн</b>\n"
                        f"🔋 На Ваш рахунок успешно зараховано: <b>{kwh_to_add} кВт·год</b>\n\n"
                        f"⚡ Можете надсилати ID станції. Приємної зарядки з eVolt UA!"
                    ),
                    parse_mode="HTML"
                )
            except Exception as tg_err:
                logger.error(f"Не вдалося надіслати сповіщення водію {user_id} в Telegram: {tg_err}")
        else:
            logger.warning(
                f"Отримано платіж на {amount_uah} грн, але коментар не є Telegram ID: '{comment_raw}'"
            )

    return Response(status_code=status.HTTP_200_OK)
