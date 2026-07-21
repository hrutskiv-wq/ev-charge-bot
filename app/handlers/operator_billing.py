"""
Хендлери кабінету оператора — частина білінгу, що живе в Telegram (Промпт 4).

Вхід: команда /operator або кнопка «🏷️ Мій білінг» головного меню. Новий
Telegram-акаунт -> онбординг (назва, телефон) -> запис у operators зі
статусом 'pending' і сповіщення LOGS_CHAT_ID; автоактивації немає навмисно —
активує адмін вручну через set_operator_status(). Далі — підключення
еквайрингу, майстер станцій, перелік/тарифи/статус станцій, виручка й
CSV-експорт. Підтвердження «увімкнув станцію» з пуша про оплату (Промпт 2b)
лишається нижче без змін.

ПОРЯДОК РОУТЕРІВ (важливо, app/main.py): цей router зареєстрований ПЕРЕД
user_router, бо той закінчується хендлером-приймачем "будь-який текст без
'/' -> ШІ-чат" (StateFilter("*")) — якби operator_billing_router йшов
пізніше, жоден вільнотекстовий крок майстра (назва станції, токен тощо)
до нього просто не доходив би. Через це кожен вільнотекстовий FSM-хендлер
тут явно виключає повідомлення, що починаються з "/", інакше /start і
/operator, надіслані просто щоб вийти з майстра, самі перетворювались би на
"назву станції" чи "токен" замість того, щоб дійти до свого справжнього
хендлера в user.py.

Мультитенантність: operator_id ЗАВЖДИ береться з
get_operator_by_telegram_id(from_user.id) того, хто натиснув/написав. Нові
callback_data (opm:/opst:/oprev:/opcsv:) свідомо НЕ несуть operator_id
всередині — лише station_id чи період, тож підмінити чужого оператора
підбором callback_data неможливо: сам operator_id для запиту в repo
береться виключно з поточного telegram-акаунта.
"""
import csv
import io
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.core.crypto import encrypt_secret
from app.core.loader import bot
from app.database import operators_repo as repo
from app.keyboards.operator import (
    get_cabinet_menu, get_revenue_csv_keyboard, get_revenue_period_keyboard,
    get_station_detail_keyboard, get_station_list_keyboard,
)
from app.services.operator_notify import CONFIRM_PREFIX
from app.services.qr_image import generate_station_qr_png
from app.states.operator_states import (
    MonobankConnect, OperatorOnboarding, StationWizard, TariffEdit,
)

logger = logging.getLogger(__name__)

router = Router()

# Публічний URL сервісу для QR (та сама логіка, що й app/api/driver_qr.py).
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL") or os.getenv("EMSP_BASE_URL") or "https://evolt.ua"
).rstrip("/")

_OPERATOR_STATUS_NOTES = {
    "pending": "⏳ Заявку на розгляді. Оплати водіїв поки не приймаються, "
               "але еквайринг і станції можна налаштувати заздалегідь.",
    "suspended": "⛔ Кабінет призупинено. Зверніться до підтримки.",
}
_STATION_STATUS_LABELS = {"active": "🟢 активна", "offline": "🟡 офлайн", "disabled": "⚪ вимкнена"}
_PERIOD_LABELS = {"today": "сьогодні", "week": "тиждень", "month": "місяць"}


def _is_free_text(message: Message) -> bool:
    """Вільний текст, а не команда — команди мають дійти до свого хендлера навіть посеред майстра."""
    return bool(message.text) and not message.text.startswith("/")


def _parse_skip(text: str):
    """'-' (після strip) -> поле пропущено (None); інакше — сам текст."""
    stripped = text.strip()
    return None if stripped == "-" else stripped


def _parse_positive_decimal(raw: str):
    try:
        value = Decimal(raw.strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


async def _resolve_operator_for(telegram_id: int):
    return await repo.get_operator_by_telegram_id(telegram_id)


# ---------------------------------------------------------------------------
# Вхід у кабінет / онбординг
# ---------------------------------------------------------------------------

async def _send_cabinet_home(target, operator, edit: bool = False):
    """target — Message (нове повідомлення) або Message з callback (редагування)."""
    has_token = bool(await repo.get_operator_monobank_token_encrypted(operator["id"]))
    note = _OPERATOR_STATUS_NOTES.get(operator["status"])
    text = f"🏷️ <b>Кабінет оператора «{operator['name']}»</b>"
    if note:
        text += f"\n{note}"
    kb = get_cabinet_menu(has_token)
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


async def _open_cabinet(message: Message, state: FSMContext):
    await state.clear()
    operator = await _resolve_operator_for(message.from_user.id)
    if operator is None:
        await state.set_state(OperatorOnboarding.waiting_for_name)
        await message.answer(
            "🏷️ <b>Кабінет оператора зарядних станцій</b>\n\n"
            "Ще не зареєстровані. Введіть назву вашого бізнесу/мережі станцій "
            "(наприклад: «Готель Едем»):",
            parse_mode="HTML",
        )
        return
    await _send_cabinet_home(message, operator)


@router.message(Command("operator"), StateFilter("*"))
async def cmd_operator_cabinet(message: Message, state: FSMContext):
    await _open_cabinet(message, state)


@router.message(F.text == "🏷️ Мій білінг", StateFilter("*"))
async def button_operator_cabinet(message: Message, state: FSMContext):
    await _open_cabinet(message, state)


async def _notify_admins_new_operator(operator_id: int, name: str, phone: str, from_user):
    chat_id = os.getenv("LOGS_CHAT_ID")
    if not chat_id:
        return
    username = f"@{from_user.username}" if from_user.username else str(from_user.id)
    text = (
        "🆕 <b>Новий оператор на модерації</b>\n"
        f"ID: <code>{operator_id}</code>\n"
        f"Назва: {name}\n"
        f"Телефон: {phone}\n"
        f"Telegram: {username} (id {from_user.id})\n\n"
        f"Активація вручну: set_operator_status({operator_id}, 'active')"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as e:
        # Сповіщення адміну не має ламати сам онбординг — заявка вже
        # записана в operators, адмін просто дізнається про неї пізніше
        # (напр. переглянувши таблицю руками).
        logger.error("Не вдалося сповістити LOGS_CHAT_ID про нового оператора %s: %s",
                     operator_id, e)


@router.message(StateFilter(OperatorOnboarding.waiting_for_name), _is_free_text)
async def onboarding_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Назва не може бути порожньою. Введіть назву ще раз:")
        return
    if len(name) > 255:
        await message.answer("Задовга назва (максимум 255 символів). Скоротіть і надішліть ще раз:")
        return
    await state.update_data(name=name)
    await state.set_state(OperatorOnboarding.waiting_for_phone)
    await message.answer("Телефон для звʼязку (наприклад: +380501234567):")


@router.message(StateFilter(OperatorOnboarding.waiting_for_phone), _is_free_text)
async def onboarding_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not phone:
        await message.answer("Телефон не може бути порожнім. Введіть ще раз:")
        return
    if len(phone) > 32:
        await message.answer("Задовгий номер (максимум 32 символи). Перевірте формат і надішліть ще раз:")
        return

    data = await state.get_data()
    name = (data.get("name") or "").strip()
    await state.clear()

    operator_id = await repo.create_operator(name, message.from_user.id, phone=phone)
    if operator_id is None:
        await message.answer("Ви вже зареєстровані. Наберіть /operator, щоб відкрити кабінет.")
        return

    await message.answer(
        "✅ Заявку подано! Очікуйте підтвердження — ми напишемо, коли акаунт "
        "активовано. До того часу можна одразу підключити еквайринг і додати "
        "станції через /operator: платежі почнуть прийматись одразу після "
        "активації."
    )
    await _notify_admins_new_operator(operator_id, name, phone, message.from_user)


# ---------------------------------------------------------------------------
# Підключення еквайрингу Monobank
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "opm:token", StateFilter("*"))
async def cabinet_connect_token(callback: CallbackQuery, state: FSMContext):
    operator = await _resolve_operator_for(callback.from_user.id)
    if operator is None:
        await callback.answer("Спершу зареєструйтесь: /operator", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Токен приймається лише в приватному чаті з ботом.", show_alert=True)
        return
    await state.set_state(MonobankConnect.waiting_for_token)
    await callback.answer()
    await callback.message.answer(
        "Надішліть токен мерчанта Monobank Acquiring (Особистий кабінет банку → "
        "Мерчанти → Токен). Повідомлення з токеном буде видалено одразу після "
        "збереження — ніде, крім зашифрованого запису в базі, він не лишиться."
    )


async def _try_delete_token_message(message: Message, operator_id: int = None):
    """
    Прибирає повідомлення з відкритим токеном з історії чату. Викликається
    з обох гілок save_monobank_token() — і успішної, і аномальної (не
    приватний чат) — токен не можна лишати видимим в жодному разі.
    """
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception as e:
        logger.info("Не вдалося видалити повідомлення з токеном оператора %s: %s",
                    operator_id, e)


@router.message(StateFilter(MonobankConnect.waiting_for_token), _is_free_text)
async def save_monobank_token(message: Message, state: FSMContext):
    await state.clear()
    if message.chat.type != "private":
        # Захисний дубль поверх перевірки при вході в стан — кнопка вже мала
        # відсіяти групові чати, це аномальний шлях. Але якщо сюди все ж
        # дійшли, повідомлення з токеном так само не можна лишати в групі.
        await _try_delete_token_message(message)
        await message.answer("Токен приймається лише в приватному чаті з ботом.")
        return

    operator = await _resolve_operator_for(message.from_user.id)
    token = message.text.strip()

    if operator is not None and token:
        encrypted = encrypt_secret(token)
        await repo.set_operator_monobank_token(operator["id"], encrypted)

    # Незалежно від того, чи вдалось зберегти токен вище.
    await _try_delete_token_message(message, operator["id"] if operator else None)

    if operator is None:
        await message.answer("Спершу зареєструйтесь: /operator")
        return
    if not token:
        await message.answer("Порожній токен. Спробуйте ще раз через кнопку «Підключити еквайринг».")
        return

    tail = token[-4:] if len(token) >= 4 else token
    await message.answer(f"✅ Токен збережено, закінчується на …{tail}.")


# ---------------------------------------------------------------------------
# Майстер станції
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "opm:add_station", StateFilter("*"))
async def cabinet_add_station_start(callback: CallbackQuery, state: FSMContext):
    operator = await _resolve_operator_for(callback.from_user.id)
    if operator is None:
        await callback.answer("Спершу зареєструйтесь: /operator", show_alert=True)
        return
    await state.set_state(StationWizard.waiting_for_name)
    await callback.answer()
    await callback.message.answer(
        "➕ <b>Нова станція</b>\n\nНазва станції (наприклад: «Готель Едем — паркінг»):",
        parse_mode="HTML",
    )


@router.message(StateFilter(StationWizard.waiting_for_name), _is_free_text)
async def station_wizard_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or name == "-":
        await message.answer("Назва обовʼязкова. Введіть назву станції:")
        return
    if len(name) > 255:
        await message.answer("Задовга назва (максимум 255 символів). Скоротіть:")
        return
    await state.update_data(name=name)
    await state.set_state(StationWizard.waiting_for_address)
    await message.answer("Адреса станції (або «-», щоб пропустити):")


@router.message(StateFilter(StationWizard.waiting_for_address), _is_free_text)
async def station_wizard_address(message: Message, state: FSMContext):
    await state.update_data(address=_parse_skip(message.text))
    await state.set_state(StationWizard.waiting_for_connector)
    await message.answer("Тип конектора, наприклад Type 2 / CCS / GBT / Schuko (або «-»):")


@router.message(StateFilter(StationWizard.waiting_for_connector), _is_free_text)
async def station_wizard_connector(message: Message, state: FSMContext):
    await state.update_data(connector_type=_parse_skip(message.text))
    await state.set_state(StationWizard.waiting_for_power)
    await message.answer("Потужність, кВт, наприклад 22 (або «-»):")


@router.message(StateFilter(StationWizard.waiting_for_power), _is_free_text)
async def station_wizard_power(message: Message, state: FSMContext):
    raw = _parse_skip(message.text)
    power = None
    if raw is not None:
        parsed = _parse_positive_decimal(raw)
        if parsed is None:
            await message.answer("Потужність має бути додатним числом, наприклад 22 (або «-», щоб пропустити):")
            return
        power = float(parsed)  # FSM-стан мусить бути JSON-серіалізовним (RedisStorage у проді) — не Decimal
    await state.update_data(power_kw=power)
    await state.set_state(StationWizard.waiting_for_tariff_kwh)
    await message.answer("Тариф, грн за 1 кВт·год, наприклад 12.50:")


@router.message(StateFilter(StationWizard.waiting_for_tariff_kwh), _is_free_text)
async def station_wizard_tariff_kwh(message: Message, state: FSMContext):
    tariff = _parse_positive_decimal(message.text)
    if tariff is None:
        await message.answer("Тариф має бути додатним числом, наприклад 12.50:")
        return
    await state.update_data(tariff_uah_kwh=float(tariff))
    await state.set_state(StationWizard.waiting_for_tariff_start)
    await message.answer("Плата за старт сесії, грн (необовʼязково — «-», щоб пропустити):")


async def _send_new_station_qr(message: Message, station_name: str, qr_slug: str):
    url = f"{PUBLIC_BASE_URL}/s/{qr_slug}"
    png = generate_station_qr_png(url)
    await message.answer_photo(
        BufferedInputFile(png, filename=f"qr_{qr_slug}.png"),
        caption=(
            f"✅ Станцію «{station_name}» додано.\n\n"
            f"QR: <code>{url}</code>\n\n"
            "Роздрукуйте цей код і розмістіть на станції — водій сканує його для оплати."
        ),
        parse_mode="HTML",
    )


@router.message(StateFilter(StationWizard.waiting_for_tariff_start), _is_free_text)
async def station_wizard_tariff_start(message: Message, state: FSMContext):
    raw = _parse_skip(message.text)
    tariff_start = None
    if raw is not None:
        parsed = _parse_positive_decimal(raw)
        if parsed is None:
            await message.answer("Плата за старт має бути додатним числом (або «-», щоб пропустити):")
            return
        tariff_start = float(parsed)

    data = await state.get_data()
    await state.clear()

    operator = await _resolve_operator_for(message.from_user.id)
    if operator is None:
        await message.answer("Спершу зареєструйтесь: /operator")
        return

    station_id, qr_slug = await repo.create_station(
        operator["id"], data["name"], data["tariff_uah_kwh"],
        address=data.get("address"), connector_type=data.get("connector_type"),
        power_kw=data.get("power_kw"), tariff_uah_start=tariff_start,
    )
    await _send_new_station_qr(message, data["name"], qr_slug)


# ---------------------------------------------------------------------------
# Перелік / картка станції
# ---------------------------------------------------------------------------

def _station_detail_text(station) -> str:
    lines = [
        f"🔌 <b>{station['name']}</b>",
        f"Статус: {_STATION_STATUS_LABELS.get(station['status'], station['status'])}",
        f"Тариф: {station['tariff_uah_kwh']} грн/кВт·год",
    ]
    if station.get("tariff_uah_start"):
        lines.append(f"Плата за старт: {station['tariff_uah_start']} грн")
    if station.get("address"):
        lines.append(f"Адреса: {station['address']}")
    if station.get("connector_type"):
        lines.append(f"Конектор: {station['connector_type']}")
    lines.append(f"QR: <code>{PUBLIC_BASE_URL}/s/{station['qr_slug']}</code>")
    return "\n".join(lines)


@router.callback_query(F.data == "opm:home", StateFilter("*"))
async def cabinet_home(callback: CallbackQuery, state: FSMContext):
    operator = await _resolve_operator_for(callback.from_user.id)
    if operator is None:
        await callback.answer("Спершу зареєструйтесь: /operator", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _send_cabinet_home(callback.message, operator, edit=True)


@router.callback_query(F.data == "opm:stations", StateFilter("*"))
async def cabinet_station_list(callback: CallbackQuery):
    operator = await _resolve_operator_for(callback.from_user.id)
    if operator is None:
        await callback.answer("Спершу зареєструйтесь: /operator", show_alert=True)
        return
    stations = await repo.list_stations(operator["id"])
    await callback.answer()
    if not stations:
        await callback.message.edit_text(
            "У вас поки немає жодної станції. Додайте першу з кабінету.",
            reply_markup=get_station_list_keyboard([]),
        )
        return
    await callback.message.edit_text(
        "🔌 <b>Мої станції</b>\n\nОберіть станцію:",
        parse_mode="HTML", reply_markup=get_station_list_keyboard(stations),
    )


@router.callback_query(F.data.startswith("opst:"), StateFilter("*"))
async def station_action(callback: CallbackQuery, state: FSMContext):
    try:
        _prefix, station_id_raw, action = callback.data.split(":")
        station_id = int(station_id_raw)
    except ValueError:
        await callback.answer("Некоректна кнопка", show_alert=True)
        return

    operator = await _resolve_operator_for(callback.from_user.id)
    if operator is None:
        await callback.answer("Спершу зареєструйтесь: /operator", show_alert=True)
        return

    # Станція шукається В МЕЖАХ саме цього operator_id — чужий station_id
    # (навіть підставлений навмисно в callback_data) поверне None.
    station = await repo.get_station(operator["id"], station_id)
    if station is None:
        await callback.answer("Станцію не знайдено", show_alert=True)
        return

    if action == "view":
        await callback.answer()
        await callback.message.edit_text(
            _station_detail_text(station), parse_mode="HTML",
            reply_markup=get_station_detail_keyboard(station_id, station["status"]),
        )
    elif action == "toggle":
        new_status = "disabled" if station["status"] == "active" else "active"
        await repo.set_station_status(operator["id"], station_id, new_status)
        station = await repo.get_station(operator["id"], station_id)
        await callback.answer(f"Статус змінено: {_STATION_STATUS_LABELS.get(new_status, new_status)}")
        await callback.message.edit_text(
            _station_detail_text(station), parse_mode="HTML",
            reply_markup=get_station_detail_keyboard(station_id, station["status"]),
        )
    elif action == "qr":
        await callback.answer()
        png = generate_station_qr_png(f"{PUBLIC_BASE_URL}/s/{station['qr_slug']}")
        await callback.message.answer_photo(
            BufferedInputFile(png, filename=f"qr_{station['qr_slug']}.png"),
            caption=f"QR станції «{station['name']}»",
        )
    elif action == "tariff":
        await state.set_state(TariffEdit.waiting_for_new_tariff)
        await state.update_data(station_id=station_id)
        await callback.answer()
        await callback.message.answer(
            f"Новий тариф грн/кВт·год для станції «{station['name']}» "
            f"(зараз {station['tariff_uah_kwh']}):"
        )
    else:
        await callback.answer("Невідома дія", show_alert=True)


@router.message(StateFilter(TariffEdit.waiting_for_new_tariff), _is_free_text)
async def tariff_edit_apply(message: Message, state: FSMContext):
    tariff = _parse_positive_decimal(message.text)
    if tariff is None:
        await message.answer("Тариф має бути додатним числом, наприклад 12.50:")
        return

    data = await state.get_data()
    station_id = data.get("station_id")
    await state.clear()

    operator = await _resolve_operator_for(message.from_user.id)
    if operator is None or station_id is None:
        await message.answer("Спершу зареєструйтесь: /operator")
        return

    station = await repo.get_station(operator["id"], station_id)
    if station is None:
        await message.answer("Станцію не знайдено — можливо, її видалили.")
        return

    tariff_start = station.get("tariff_uah_start")
    await repo.update_station_tariff(
        operator["id"], station_id, float(tariff),
        tariff_uah_start=float(tariff_start) if tariff_start is not None else None,
    )
    await message.answer(f"✅ Тариф оновлено: {tariff} грн/кВт·год.")


# ---------------------------------------------------------------------------
# Виручка та CSV-експорт
# ---------------------------------------------------------------------------

def _period_since(period: str, now: datetime = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    raise ValueError(f"Невідомий період: {period}")


def _summarize_ledger(summary: dict):
    """{'session_income': ..., 'platform_commission': ...} -> (оборот, комісія, до виплати)."""
    gross = Decimal(str(summary.get("session_income") or 0))
    commission = Decimal(str(summary.get("platform_commission") or 0))  # вже від'ємна
    return gross, commission, gross + commission


_CSV_HEADER = ["id", "дата", "тип", "сума_грн", "сесія", "опис"]


def _build_ledger_csv(rows) -> bytes:
    """CSV журналу за період. UTF-8 з BOM — щоб Excel коректно показував кирилицю."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_HEADER)
    for row in rows:
        created_at = row["created_at"]
        writer.writerow([
            row["id"],
            created_at.strftime("%Y-%m-%d %H:%M") if created_at else "",
            row["type"],
            row["amount_uah"],
            row["session_id"] if row["session_id"] is not None else "",
            row["description"] or "",
        ])
    return buf.getvalue().encode("utf-8-sig")


@router.callback_query(F.data == "opm:revenue", StateFilter("*"))
async def cabinet_revenue_menu(callback: CallbackQuery):
    operator = await _resolve_operator_for(callback.from_user.id)
    if operator is None:
        await callback.answer("Спершу зареєструйтесь: /operator", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "💰 <b>Виручка</b>\n\nОберіть період:", parse_mode="HTML",
        reply_markup=get_revenue_period_keyboard(),
    )


@router.callback_query(F.data.startswith("oprev:"), StateFilter("*"))
async def cabinet_revenue_period(callback: CallbackQuery):
    period = callback.data.split(":", 1)[1]
    if period not in _PERIOD_LABELS:
        await callback.answer("Невідомий період", show_alert=True)
        return

    operator = await _resolve_operator_for(callback.from_user.id)
    if operator is None:
        await callback.answer("Спершу зареєструйтесь: /operator", show_alert=True)
        return

    since = _period_since(period)
    summary = await repo.get_ledger_summary(operator["id"], since)
    gross, commission, net = _summarize_ledger(summary)
    sessions = await repo.list_sessions(operator["id"], limit=5)

    lines = [
        f"💰 <b>Виручка за {_PERIOD_LABELS[period]}</b>",
        "",
        f"Оборот: <b>{gross:.2f} грн</b>",
        f"Комісія платформи: <b>{commission:.2f} грн</b>",
        f"До виплати: <b>{net:.2f} грн</b>",
    ]
    if sessions:
        lines += ["", "📜 <b>Останні сесії:</b>"]
        for s in sessions[:5]:
            amount = s["amount_uah"] if s["amount_uah"] is not None else 0
            lines.append(f"#{s['id']} · {s['status']} · {amount} грн")

    await callback.answer()
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=get_revenue_csv_keyboard(period),
    )


@router.callback_query(F.data.startswith("opcsv:"), StateFilter("*"))
async def cabinet_revenue_csv(callback: CallbackQuery):
    period = callback.data.split(":", 1)[1]
    if period not in _PERIOD_LABELS:
        await callback.answer("Невідомий період", show_alert=True)
        return

    operator = await _resolve_operator_for(callback.from_user.id)
    if operator is None:
        await callback.answer("Спершу зареєструйтесь: /operator", show_alert=True)
        return

    since = _period_since(period)
    rows = await repo.list_ledger_since(operator["id"], since)
    csv_bytes = _build_ledger_csv(rows)

    await callback.answer()
    await callback.message.answer_document(
        BufferedInputFile(csv_bytes, filename=f"vyruchka_{period}.csv"),
        caption=f"Виручка за {_PERIOD_LABELS[period]}",
    )


# ---------------------------------------------------------------------------
# Підтвердження «увімкнув станцію» (Промпт 2b, без змін)
# ---------------------------------------------------------------------------

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
