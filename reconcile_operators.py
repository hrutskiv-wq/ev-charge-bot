"""
Реконсиляція операторського білінгу (Промпт 5).

Добирає два свідомо відкладені вікна платіжного ланцюга White-Label
білінгу, задокументовані коментарями в коді Промптів 2a/2b:

  1. pending-платежі старші за INVOICE_TTL (app/services/monobank_acquiring
     .INVOICE_TTL_SECONDS, 15 хв) — банк уже мав дати фінальну відповідь.
     Перепитуємо його токеном оператора й доводимо ланцюг до кінця ТИМ
     САМИМ шляхом, що й webhook (apply_bank_status() з
     app/api/operator_webhook.py) — успіх нараховує дохід повним
     ланцюгом, невдача просто фіксує статус.
  2. success-платежі без запису доходу в журналі — слід перерваного
     ланцюга «платіж -> success, процес впав до сесія->paid і доходу»
     (app/api/operator_webhook.py). Доводимо тим самим ідемпотентним
     complete_paid_session().
  3. success-платежі без привʼязаної сесії — нараховувати дохід нікуди,
     лише алерт на ручний розбір.
  4. pending-сесії без payment_id старші за STALE_SESSION_MINUTES (год.) —
     слід вікна «інвойс у банку створено, наш рядок operator_payments не
     встиг записатись» (app/api/driver_qr.py). Автоматично тут нічого не
     виправити (invoice_id нам невідомий) — алерт зі списком для звірки
     з випискою банку.

ІДЕМПОТЕНТНО: два запуски поспіль дають той самий результат без подвійних
нарахувань. Це не окрема властивість самого скрипта — вся платіжна логіка,
яку він викликає (apply_bank_status/complete_paid_session,
record_session_income), вже ідемпотентна сама по собі (Промпт 2a/2b);
звірка лише знаходить, до чого її застосувати повторно.

Токени операторів розшифровуються лише в памʼяті процесу (app.core.crypto),
ніколи не логуються. Банк недоступний або токен не розшифрувався для
ОДНОГО оператора -> цей оператор пропускається (з алертом), решта
звіряються далі — один зламаний токен не має зривати весь прогін.

Використання:
    python reconcile_operators.py [--stale-minutes N]   # за замовчуванням 60
    docker compose exec bot python reconcile_operators.py

Код виходу: 0 — усе гаразд або виправлено автоматично; 1 — є пункти, що
потребують ручного розбору, або банк був недоступний для когось з
операторів (зручно для cron + моніторингу, той самий контракт, що й
reconcile_payments.py).
"""
import argparse
import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

from app.api.operator_webhook import apply_bank_status, complete_paid_session
from app.core.crypto import EncryptionKeyMissing, decrypt_secret
from app.database import operators_repo as repo
from app.database.connection import close_postgres, init_postgres
from app.services.monobank_acquiring import (
    INVOICE_TTL_SECONDS,
    MonobankError,
    get_invoice_status,
)

logger = logging.getLogger(__name__)

# Скільки має пройти від створення pending-сесії без payment_id, щоб
# вважати її слідом вікна (2), а не водієм, що просто ще вводить суму на
# сторінці /s/{qr_slug}.
STALE_SESSION_MINUTES = 60

# Виправлені автоматично (не потребують людини).
_FIXED_OUTCOMES = {"credited", "status_updated"}
# Race з паралельним webhook — не проблема, звірка тут ні до чого.
_BENIGN_OUTCOMES = {"already_processed"}


class ReconcileStats:
    def __init__(self):
        self.checked = 0
        self.fixed = 0
        self.manual = []            # [{type, operator_id, operator_name, ...}]
        self.bank_unavailable = []  # [operator_id, ...] — дедуплікується при друці


def _manual_item(item_type: str, operator, **fields) -> dict:
    return {"type": item_type, "operator_id": operator["id"],
            "operator_name": operator["name"], **fields}


async def _process_pending_payments(operator, token, pending, stats: ReconcileStats):
    for payment in pending:
        stats.checked += 1
        try:
            invoice = await get_invoice_status(token, payment["invoice_id"])
        except MonobankError as e:
            logger.error("Оператор %s: банк недоступний для інвойсу %s: %s",
                         operator["id"], payment["invoice_id"], e)
            if operator["id"] not in stats.bank_unavailable:
                stats.bank_unavailable.append(operator["id"])
            # Банк, найімовірніше, недоступний загалом, а не для цього
            # конкретного інвойсу — решту pending-платежів цього оператора
            # залишаємо наступному прогону, а не довбемось у той самий збій.
            return

        outcome = await apply_bank_status(operator["id"], payment, invoice)
        if outcome in _FIXED_OUTCOMES:
            stats.fixed += 1
        elif outcome in _BENIGN_OUTCOMES:
            continue
        else:
            # 'no_session' / 'amount_mismatch' / 'unknown_status' (банк і
            # досі не дав фінальної відповіді, хоча TTL уже сплив).
            stats.manual.append(_manual_item(
                f"pending_payment_{outcome}", operator,
                payment_id=payment["id"], invoice_id=payment["invoice_id"],
                amount_uah=payment["amount_uah"],
            ))


async def _reconcile_missing_income(operator, stats: ReconcileStats):
    rows = await repo.list_success_payments_without_income(operator["id"])
    for row in rows:
        stats.checked += 1
        outcome = await complete_paid_session(operator["id"], row)
        if outcome == "credited":
            stats.fixed += 1
        else:
            stats.manual.append(_manual_item(
                f"success_without_income_{outcome}", operator,
                payment_id=row["id"], invoice_id=row["invoice_id"],
                amount_uah=row["amount_uah"],
            ))


async def _reconcile_orphan_payments(operator, stats: ReconcileStats):
    rows = await repo.list_success_payments_without_session(operator["id"])
    for row in rows:
        stats.checked += 1
        stats.manual.append(_manual_item(
            "success_without_session", operator,
            payment_id=row["id"], invoice_id=row["invoice_id"],
            amount_uah=row["amount_uah"],
        ))


async def _reconcile_stale_sessions(operator, stats: ReconcileStats, cutoff):
    rows = await repo.list_stale_pending_sessions_without_payment(operator["id"], cutoff)
    for row in rows:
        stats.checked += 1
        stats.manual.append(_manual_item(
            "stale_session_without_payment", operator,
            session_id=row["id"], station_name=row["station_name"],
            amount_uah=row["amount_uah"],
        ))


async def reconcile_operator(operator, stats: ReconcileStats, payment_cutoff, session_cutoff):
    """Прогін усіх 4 сценаріїв для ОДНОГО оператора — межа ізоляції звірки."""
    pending = await repo.list_pending_payments_older_than(operator["id"], payment_cutoff)

    token = None
    token_encrypted = await repo.get_operator_monobank_token_encrypted(operator["id"])
    if token_encrypted:
        try:
            token = decrypt_secret(token_encrypted)
        except (EncryptionKeyMissing, ValueError) as e:
            logger.error("Оператор %s: не вдалося розшифрувати токен: %s", operator["id"], e)

    if token:
        await _process_pending_payments(operator, token, pending, stats)
    elif pending:
        logger.error(
            "Оператор %s: немає робочого еквайринг-токена — %s pending-платежів "
            "не перевірено банком", operator["id"], len(pending),
        )
        stats.bank_unavailable.append(operator["id"])

    await _reconcile_missing_income(operator, stats)
    await _reconcile_orphan_payments(operator, stats)
    await _reconcile_stale_sessions(operator, stats, session_cutoff)


def _format_manual_line(item: dict) -> str:
    op = f"оператор #{item['operator_id']} «{item['operator_name']}»"
    if item["type"].startswith("pending_payment_") or item["type"].startswith("success_without_income_"):
        return (f"   [{item['type']}] {op}: платіж #{item['payment_id']} "
                f"(інвойс {item['invoice_id']}) на {item['amount_uah']} грн")
    if item["type"] == "success_without_session":
        return (f"   [{item['type']}] {op}: платіж #{item['payment_id']} "
                f"(інвойс {item['invoice_id']}) на {item['amount_uah']} грн — сесії немає")
    if item["type"] == "stale_session_without_payment":
        return (f"   [{item['type']}] {op}: сесія #{item['session_id']} "
                f"на станції «{item['station_name']}», {item['amount_uah']} грн")
    return f"   [{item['type']}] {op}: {item}"


def _print_summary(stats: ReconcileStats):
    print(f"\n=== Звірка операторського білінгу ({datetime.now(timezone.utc).isoformat()}) ===\n")
    print(f"Перевірено пунктів: {stats.checked}")
    print(f"Виправлено автоматично: {stats.fixed}")
    print(f"Потребує ручного розбору: {len(stats.manual)}")
    if stats.manual:
        for item in stats.manual:
            print(_format_manual_line(item))
    if stats.bank_unavailable:
        print(f"\n⚠️ Банк недоступний / немає токена для операторів: "
              f"{sorted(set(stats.bank_unavailable))}")
    print()


def _get_bot():
    """
    Винесено окремою функцією, щоб тести могли підмінити джерело `bot` без
    залежності від реального app.core.loader (створення Bot() там падає без
    валідного BOT_TOKEN, а сам виклик — мережевий).
    """
    from app.core.loader import bot
    return bot


async def _push_summary_to_telegram(stats: ReconcileStats):
    chat_id = os.getenv("LOGS_CHAT_ID")
    if not chat_id:
        return
    try:
        bot = _get_bot()

        lines = [
            "🔎 <b>Звірка операторського білінгу</b>",
            f"Перевірено: {stats.checked}",
            f"Виправлено автоматично: {stats.fixed}",
            f"Потребує ручного розбору: {len(stats.manual)}",
        ]
        for item in stats.manual[:30]:
            lines.append(html.escape(_format_manual_line(item).strip()))
        if len(stats.manual) > 30:
            lines.append(f"… і ще {len(stats.manual) - 30} пунктів (див. лог сервера)")
        if stats.bank_unavailable:
            lines.append(
                "⚠️ Банк недоступний / немає токена для операторів: "
                + html.escape(str(sorted(set(stats.bank_unavailable))))
            )

        await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
    except Exception as e:
        # Збій пуша в Telegram не має ламати саму звірку — вона вже
        # завершилась і надрукувала підсумок у консоль.
        logger.error("Не вдалося надіслати підсумок звірки в LOGS_CHAT_ID: %s", e)


async def run(stale_minutes: int, now: datetime = None) -> int:
    """
    `now` — лише для тестів (детермінований "поточний момент" замість
    реального годинника); прод-запуск завжди йде без нього.
    """
    now = now or datetime.now(timezone.utc)
    payment_cutoff = now - timedelta(seconds=INVOICE_TTL_SECONDS)
    session_cutoff = now - timedelta(minutes=stale_minutes)

    stats = ReconcileStats()
    await init_postgres()
    try:
        operators = await repo.list_operators()
        for operator in operators:
            await reconcile_operator(operator, stats, payment_cutoff, session_cutoff)
    finally:
        await close_postgres()

    _print_summary(stats)
    await _push_summary_to_telegram(stats)

    problems = bool(stats.manual) or bool(stats.bank_unavailable)
    return 1 if problems else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Реконсиляція операторського білінгу.")
    parser.add_argument("--stale-minutes", type=int, default=STALE_SESSION_MINUTES,
                        help="Поріг для pending-сесій без payment_id, у хвилинах "
                             f"(за замовчуванням {STALE_SESSION_MINUTES}).")
    args = parser.parse_args()

    exit_code = asyncio.run(run(args.stale_minutes))
    raise SystemExit(exit_code)
