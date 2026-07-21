"""
QR-флоу оплати водія (Промпт 2b).

    GET  /s/{qr_slug}                      сторінка станції: тариф, статус, сума
    POST /s/{qr_slug}/start                створення інвойсу -> редірект у Monobank
    GET  /s/{qr_slug}/receipt/{session_id} чек

Водій НЕ реєструється: єдиний ключ доступу — сам qr_slug, надрукований під
QR-кодом на станції. Тому:

  * жодна сторінка не приймає operator_id ззовні — він завжди береться зі
    станції, знайденої за slug;
  * чек перевіряє, що сесія належить САМЕ цій станції, інакше підбором
    session_id можна було б читати чужі сесії того ж оператора.

Гроші йдуть напряму оператору: інвойс створюється токеном його мерчанта
(див. app/services/monobank_acquiring.py), ми лише софт.
"""
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.crypto import EncryptionKeyMissing, decrypt_secret
from app.database import operators_repo as repo
from app.services.monobank_acquiring import MonobankError, create_invoice

logger = logging.getLogger(__name__)

driver_router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent.parent / "templates"))

# Публічний URL сервісу — потрібен банку для redirectUrl/webHookUrl, тому
# це має бути адреса, доступна ЗЗОВНІ, а не localhost. EMSP_BASE_URL уже є в
# оточенні для OCPI і вказує туди ж, тож використовуємо його як запасний.
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL") or os.getenv("EMSP_BASE_URL") or "https://evolt.ua"
).rstrip("/")

# Готові суми на сторінці. Середня — вибрана за замовчуванням.
AMOUNT_PRESETS = [100, 200, 300]
MIN_AMOUNT = Decimal("20")
MAX_AMOUNT = Decimal("5000")

_STATUS_LABELS = {
    "active": "Вільна",
    "offline": "Немає звʼязку",
    "disabled": "Вимкнена",
}


def _fmt(value, places="0.01") -> str:
    """Гроші/кВт·год у вигляді для людини: 12.50 -> «12.50», 12.00 -> «12»."""
    if value is None:
        return ""
    quantized = Decimal(str(value)).quantize(Decimal(places))
    normalized = quantized.normalize()
    # normalize() перетворює 100 на 1E+2 — повертаємось до звичайного запису
    return f"{normalized:f}"


def _station_context(request: Request, station, error: str = None) -> dict:
    tariff = Decimal(str(station["tariff_uah_kwh"]))
    preset_kwh = (AMOUNT_PRESETS[1] / tariff).quantize(Decimal("0.1")) if tariff > 0 else 0
    return {
        "request": request,
        "station": station,
        "available": station["status"] == "active",
        "status_label": _STATUS_LABELS.get(station["status"], station["status"]),
        "tariff_kwh": _fmt(station["tariff_uah_kwh"]),
        "tariff_start": _fmt(station["tariff_uah_start"]),
        "power_kw": _fmt(station["power_kw"], "0.1"),
        "manual": station["mode"] == "manual",
        "presets": AMOUNT_PRESETS,
        "preset_kwh": _fmt(preset_kwh, "0.1"),
        "min_amount": int(MIN_AMOUNT),
        "max_amount": int(MAX_AMOUNT),
        "error": error,
    }


def _not_found(request: Request):
    return templates.TemplateResponse(
        request=request, name="driver/not_found.html", context={}, status_code=404,
    )


@driver_router.get("/s/{qr_slug}")
async def station_page(qr_slug: str, request: Request):
    station = await repo.get_station_by_qr_slug(qr_slug)
    if station is None:
        return _not_found(request)
    return templates.TemplateResponse(
        request=request, name="driver/station.html",
        context=_station_context(request, station),
    )


def _parse_amount(amount_uah: str, custom_amount: str):
    """
    Сума з форми. custom_amount (вручну введена) має пріоритет над
    пресетом — інакше водій вписав би своє число, а заплатив би за
    радіокнопку, яка лишилась вибраною.

    Повертає (Decimal, None) або (None, текст помилки).
    """
    raw = (custom_amount or "").strip() or (amount_uah or "").strip()
    if not raw:
        return None, "Вкажіть суму оплати."
    try:
        value = Decimal(raw.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None, "Сума має бути числом."
    if not value.is_finite() or value <= 0:
        return None, "Сума має бути додатним числом."
    value = value.quantize(Decimal("0.01"))
    if value < MIN_AMOUNT:
        return None, f"Мінімальна сума — {int(MIN_AMOUNT)} грн."
    if value > MAX_AMOUNT:
        return None, f"Максимальна сума — {int(MAX_AMOUNT)} грн."
    return value, None


@driver_router.post("/s/{qr_slug}/start")
async def start_session(qr_slug: str, request: Request,
                        amount_uah: str = Form(None),
                        custom_amount: str = Form(None)):
    station = await repo.get_station_by_qr_slug(qr_slug)
    if station is None:
        return _not_found(request)

    def error_page(message: str):
        return templates.TemplateResponse(
            request=request, name="driver/station.html",
            context=_station_context(request, station, error=message),
            status_code=400,
        )

    if station["status"] != "active":
        return error_page("Станція зараз недоступна для оплати.")

    amount, amount_error = _parse_amount(amount_uah, custom_amount)
    if amount_error:
        return error_page(amount_error)

    operator_id = station["operator_id"]

    # Токен еквайрингу оператора. Без нього платити нікуди — і це помилка
    # налаштування оператора, а не водія, тому текст нейтральний.
    token_encrypted = await repo.get_operator_monobank_token_encrypted(operator_id)
    if not token_encrypted:
        logger.error("Станція %s (оператор %s): спроба оплати без збереженого "
                     "еквайринг-токена", station["id"], operator_id)
        return error_page("Оператор ще не завершив налаштування прийому оплат.")

    try:
        operator_token = decrypt_secret(token_encrypted)
    except (EncryptionKeyMissing, ValueError) as e:
        logger.error("Станція %s: не вдалося розшифрувати токен оператора %s: %s",
                     station["id"], operator_id, e)
        return error_page("Тимчасова технічна проблема. Спробуйте за хвилину.")

    # Сесія створюється ДО інвойсу: якщо оплата не дійде, лишиться
    # 'pending'-сесія, і це видно у звірці. Зворотний порядок дав би
    # оплачений інвойс без сліду в системі.
    session_id = await repo.create_session(operator_id, station["id"], amount_uah=amount)
    if session_id is None:
        logger.error("Не вдалося створити сесію для станції %s оператора %s",
                     station["id"], operator_id)
        return error_page("Тимчасова технічна проблема. Спробуйте за хвилину.")

    receipt_url = f"{PUBLIC_BASE_URL}/s/{qr_slug}/receipt/{session_id}"
    try:
        invoice = await create_invoice(
            operator_token,
            amount_uah=amount,
            reference=f"session-{session_id}",
            redirect_url=receipt_url,
            webhook_url=f"{PUBLIC_BASE_URL}/webhook/operator/{operator_id}",
            destination=f"Зарядка: {station['name']}",
        )
    except MonobankError as e:
        logger.error("Станція %s: банк не створив інвойс: %s", station["id"], e)
        await repo.set_session_status(operator_id, session_id, "failed")
        return error_page("Банк тимчасово недоступний. Спробуйте за хвилину.")

    payment_id = await repo.create_operator_payment(
        operator_id, invoice["invoiceId"], amount,
    )
    await repo.attach_payment_to_session(operator_id, session_id, payment_id)

    logger.info("🧾 Станція %s: сесія #%s, інвойс %s на %s грн",
                station["id"], session_id, invoice["invoiceId"], amount)

    # 303 — щоб повторне оновлення сторінки банку не надсилало форму знову.
    return RedirectResponse(invoice["pageUrl"], status_code=303)


@driver_router.get("/s/{qr_slug}/receipt/{session_id}")
async def receipt_page(qr_slug: str, session_id: int, request: Request):
    station = await repo.get_station_by_qr_slug(qr_slug)
    if station is None:
        return _not_found(request)

    session = await repo.get_session(station["operator_id"], session_id)
    # Сесія має належати саме цій станції: інакше, знаючи один slug, можна
    # було б перебором session_id читати чужі сесії того самого оператора.
    if session is None or session["station_id"] != station["id"]:
        return _not_found(request)

    return templates.TemplateResponse(
        request=request, name="driver/receipt.html",
        context={
            "request": request,
            "station": station,
            "session": session,
            "state": session["status"],
            "manual": station["mode"] == "manual",
            "amount": _fmt(session["amount_uah"]),
            "kwh": _fmt(session["kwh"], "0.001"),
            "started_at": session["started_at"].strftime("%d.%m.%Y %H:%M")
                          if session["started_at"] else "",
            "ended_at": session["ended_at"].strftime("%d.%m.%Y %H:%M")
                        if session["ended_at"] else "",
        },
    )
