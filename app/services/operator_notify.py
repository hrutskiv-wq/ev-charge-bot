"""
Сповіщення оператору про події білінгу.

Чому окремий модуль, а не виклик прямо у webhook: `bot` створюється в
app/core/loader.py на рівні модуля і вимагає BOT_TOKEN. Якби webhook
імпортував його зверху, кожен тест платіжної логіки тягнув би за собою
aiogram і живий токен. Тут імпорт відкладений усередину функції — платіжні
тести лишаються без Telegram, а прод поводиться так само.

Головний принцип: сповіщення НІКОЛИ не має ламати платіжний флоу. Гроші
вже прийшли, сесія вже позначена оплаченою; те, що Telegram недоступний
або оператор заблокував бота, не привід повертати банку помилку.
"""
import logging

logger = logging.getLogger(__name__)

# callback_data кнопки «Увімкнув станцію»: opsess:on:<operator_id>:<session_id>
CONFIRM_PREFIX = "opsess:on"


def build_paid_message(station_name: str, amount_uah, session_id: int,
                       driver_contact: str = None) -> str:
    lines = [
        "💳 <b>Оплачено</b>",
        "",
        f"Станція: <b>{station_name}</b>",
        f"Сума: <b>{amount_uah} грн</b>",
        f"Сесія: #{session_id}",
    ]
    if driver_contact:
        lines.append(f"Водій: {driver_contact}")
    lines += ["", "⚡ Увімкніть станцію та підтвердьте кнопкою нижче."]
    return "\n".join(lines)


def build_confirm_keyboard(operator_id: int, session_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⚡ Увімкнув станцію",
            callback_data=f"{CONFIRM_PREFIX}:{operator_id}:{session_id}",
        )
    ]])


async def notify_operator_paid(telegram_id: int, operator_id: int, session_id: int,
                               station_name: str, amount_uah,
                               driver_contact: str = None) -> bool:
    """
    «Оплачено N грн, увімкніть станцію» + кнопка підтвердження.

    Повертає True/False замість того, щоб кидати — викликач (webhook) не
    має падати через проблеми Telegram.
    """
    try:
        from app.core.loader import bot

        await bot.send_message(
            chat_id=telegram_id,
            text=build_paid_message(station_name, amount_uah, session_id, driver_contact),
            reply_markup=build_confirm_keyboard(operator_id, session_id),
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        # Оператор заблокував бота, не натискав /start, Telegram лежить —
        # усе це не має впливати на вже проведену оплату.
        logger.error(
            "Не вдалося сповістити оператора %s (telegram_id=%s) про оплату "
            "сесії #%s: %s", operator_id, telegram_id, session_id, e,
        )
        return False
