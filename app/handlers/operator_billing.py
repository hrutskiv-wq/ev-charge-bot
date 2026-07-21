"""
Хендлери кабінету оператора — частина білінгу, що живе в Telegram.

Поки що тут лише підтвердження «увімкнув станцію» з пуша про оплату
(Промпт 2b). Повний кабінет — онбординг, майстер станцій, тарифи, виручка —
Промпт 4.
"""
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.database import operators_repo as repo
from app.services.operator_notify import CONFIRM_PREFIX

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data.startswith(f"{CONFIRM_PREFIX}:"))
async def confirm_station_switched_on(callback: CallbackQuery):
    """
    Оператор натиснув «Увімкнув станцію» -> сесія переходить у 'charging',
    і водій бачить це на своїй сторінці-чеку.

    operator_id береться з callback_data, але ОБОВʼЯЗКОВО звіряється з
    telegram_id того, хто натиснув: інакше будь-хто, хто підгледів формат
    callback_data, міг би керувати чужими сесіями.
    """
    try:
        _prefix, _action, operator_id_raw, session_id_raw = callback.data.split(":")
        operator_id = int(operator_id_raw)
        session_id = int(session_id_raw)
    except (ValueError, AttributeError):
        logger.warning("Некоректний callback_data білінгу: %r", callback.data)
        await callback.answer("Некоректна кнопка", show_alert=True)
        return

    operator = await repo.get_operator_by_telegram_id(callback.from_user.id)
    if operator is None or operator["id"] != operator_id:
        logger.warning(
            "Telegram-користувач %s спробував підтвердити сесію #%s оператора %s",
            callback.from_user.id, session_id, operator_id,
        )
        await callback.answer("Ця дія доступна лише оператору станції", show_alert=True)
        return

    session = await repo.get_session(operator_id, session_id)
    if session is None:
        await callback.answer("Сесію не знайдено", show_alert=True)
        return

    if session["status"] == "charging":
        await callback.answer("Уже підтверджено")
        return

    if session["status"] != "paid":
        # Наприклад, сесію вже завершили або оплата відкотилась.
        await callback.answer(
            f"Сесія у стані «{session['status']}» — підтвердження не потрібне",
            show_alert=True,
        )
        return

    await repo.set_session_status(operator_id, session_id, "charging")
    logger.info("⚡ Оператор %s підтвердив увімкнення станції для сесії #%s",
                operator_id, session_id)

    await callback.answer("Готово")
    try:
        # Прибираємо кнопку, щоб не натиснули вдруге, і лишаємо слід у чаті.
        await callback.message.edit_text(
            f"{callback.message.html_text}\n\n✅ <b>Станцію увімкнено</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        # Повідомлення могли видалити чи воно застаре для редагування —
        # сесія вже переведена, це не привід показувати помилку оператору.
        logger.info("Не вдалося оновити повідомлення про оплату: %s", e)
