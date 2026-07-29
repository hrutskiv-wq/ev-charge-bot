"""
Звірка резервацій зарядки — модель A (Промпт 3c-i, kWh-баланс) і модель B
(Промпт 3c-ii, грн через Monobank hold/finalize/cancel).

kWh-кроки (не змінені цим бандлом):
  1. 'pending' резервації старші за --pending-minutes — RemoteStart ніколи
     не підтвердився StartTransaction. hold стоїть, водій не бачить свій
     кВт·год.
  2. 'active' резервації, чия сесія (за updated_at) не закривалась
     StopTransaction довше за --active-hours — станція перепідключилась і
     "забула" transactionId, або аварійно вимкнулась.
Обидва звільняються ПОВНИМ hold назад на kWh-баланс водія через
release_reservation_hold() — той самий виклик, що й start_charging_
session.py використовує для скасування.

uah-кроки (Промпт 3c-ii, докладно — docs/plan-3c-ii.md, розділ 5):
  1. 'awaiting_hold' застарілі за --awaiting-hold-minutes — інвойс
     створено, банк ще не підтвердив hold (або підтвердив, а вебхук про це
     не почув). СПЕРШУ перепитуємо банк (get_invoice_status) — ніколи не
     довіряємо локальному статусу: якщо банк каже 'hold', RemoteStart із
     ЦЬОГО процесу (окремого від живого uvicorn з /ocpp) НЕБЕЗПЕЧНИЙ (той
     самий клас бага, що "3c-i незапускний на проді", докладно — «Інваріант
     процесу» в docs/plan-3c-ii.md) — тому лише АЛЕРТ на ручний розбір,
     рядок лишається недоторканим. Якщо банк каже щось інше — hold ніколи
     не відбувся, безпечно позначаємо 'expired' локально.
  2. 'pending' застарілі — RemoteStart надіслано, StartTransaction не
     прийшов. СПЕРШУ перепитуємо банк: якщо ще 'hold' — атомарна ЛОКАЛЬНА
     заявка claim_reservation_for_settlement(expected_status='pending')
     ПЕРЕД cancel_invoice() (другий блокер рев'ю: без неї запізнілий
     StartTransaction міг активувати рядок, поки звірка вже скасовує його
     hold у банку — водій лишається без щойно скасованого hold посеред
     зарядки); заявку програно -> рядок пропускаємо, БЕЗ дзвінка в банк.
     Заявку виграно -> cancel_invoice() (повне повернення) + 'expired'.
     Якщо банк уже сам дійшов до success/reversed — лише синхронізуємо
     локальний статус, БЕЗ повторного виклику банку (claim там не
     потрібен — банку однаково нічого мутувати).
  3. 'active' застарілі за --active-hours (той самий поріг, що для kWh) —
     БЛОКЕР рев'ю бандла 3c-ii, закритий цим кроком: StopTransaction
     ніколи не прийшов (станція впала/водій висмикнув кабель), або крах
     МІЖ complete_ocpp_transaction() (сесія вже 'completed') і claim_
     reservation_for_settlement() (резервація ще НЕ 'settling'). Без
     цього кроку hold висів би до 9-денного автоскасування банку без
     жодного алерту, а оператор не отримав би нічого за віддану енергію.
     Перепитуємо банк: якщо ще 'hold' — СПЕРШУ атомарна ЛОКАЛЬНА заявка
     claim_reservation_for_settlement() (другий блокер рев'ю: цей самий
     крок, доданий для закриття ПЕРШОГО блокера, сам дзвонив у банк без
     заявки — станція, що повернулась з офлайну й шле накопичений
     StopTransaction, могла спричинити подвійний finalize/cancel на той
     самий інвойс паралельно з живим шляхом); заявку програно -> рядок
     пропускаємо. Заявку виграно -> дивимось на привʼязану сесію: вже
     'completed' -> рахуємо вартість тією самою чистою функцією
     compute_uah_settlement_amount, що й on_stop_transaction, і
     finalize/cancel на неї; сесії немає чи вона НЕ 'completed' (факт
     спожитого невідомий) -> cancel_invoice() на ПОВНУ суму (той самий
     принцип, що kWh-модель при kwh=None — не вигадуємо спожите). Плюс
     ERROR-лог (окремо від звичайного `released`-запису) — застрягла
     'active' резервація сама по собі вартий уваги сигнал, не рутинне
     прибирання.
  4. 'settling' застарілі (поріг SETTLING_STALE_SECONDS) — локальний
     claim (claim_reservation_for_settlement) відбувся, але сам розрахунок
     (виклик банку + запис фіналу) не завершився вчасно — крах МІЖ claim і
     дзвінком у банк, або МІЖ банком і локальним записом. Перепитуємо банк:
     якщо ще 'hold' — самі довершуємо finalize/cancel (вартість — та сама
     чиста функція compute_uah_settlement_amount, що й on_stop_transaction);
     якщо банк уже дійшов до success/reversed — лише синхронізуємо статус.

Для uah-кроків 2, 3 і 4 виклик банку (finalize_invoice/cancel_invoice) з
ЦЬОГО процесу БЕЗПЕЧНИЙ — на відміну від RemoteStart, це звичайний
вихідний HTTP-запит без залежності від _active_charge_points.

Використання:
    python reconcile_charging_reservations.py [--pending-minutes N] [--active-hours N] [--awaiting-hold-minutes N]
    docker compose exec bot python reconcile_charging_reservations.py

Код виходу: 0 — застряглих резервацій/аномалій не знайдено; 1 — знайдено
(звільнено/розраховано автоматично, або потребує ручного розбору) — не
помилка сама по собі, але сигнал моніторингу. Той самий контракт, що й
reconcile_operators.py/reconcile_payments.py (зручно для cron).
"""
import argparse
import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.crypto import EncryptionKeyMissing, decrypt_secret
from app.database import operators_repo as repo
from app.database.connection import close_postgres, init_postgres
from app.services.monobank_acquiring import (
    DEFAULT_TIMEOUT,
    MonobankError,
    cancel_invoice,
    finalize_invoice,
    get_invoice_status,
    kopecks_to_uah,
)
from app.services.ocpp_charging import compute_uah_settlement_amount

logger = logging.getLogger(__name__)

# RemoteStart -> StartTransaction — станція має відповісти майже миттєво;
# 30 хв — щедрий запас на затримки/ретраї, перш ніж вважати hold застряглим.
PENDING_STALE_MINUTES = 30
# Реальна зарядка може тривати кілька годин, тому поріг вищий, ніж для
# pending — 24 год явно за межею типової сесії.
ACTIVE_STALE_HOURS = 24
# Модель B: інвойс має свій TTL у банку (validity, той самий порядок, що
# INVOICE_TTL_SECONDS=900с у mock_monobank.py/monobank_acquiring.py для
# звичайних інвойсів) — трохи довший запас, перш ніж вважати "водій так і
# не заплатив" вартим перевірки.
AWAITING_HOLD_STALE_MINUTES = 20
# Поріг для 'settling' — МУСИТЬ бути свідомо БІЛЬШИМ за monobank_acquiring.
# DEFAULT_TIMEOUT (таймаут HTTP-запиту до банку): інакше звірка може
# втрутитися в рядок 'settling', поки живий виклик finalize_invoice()/
# cancel_invoice() (з on_stop_transaction) ще ФІЗИЧНО виконується в мережі
# — не встиг ні відповісти, ні впасти по таймауту — і обидва виклики
# підуть у банк одночасно (docs/plan-3c-ii.md, розділ 5, крок 3, правка 7
# рев'ю). assert нижче робить цю властивість самоперевіркою коду, а не
# лише коментарем.
SETTLING_STALE_SECONDS = 120
assert SETTLING_STALE_SECONDS > DEFAULT_TIMEOUT, (
    "SETTLING_STALE_SECONDS має бути більшим за monobank_acquiring.DEFAULT_TIMEOUT — "
    "інакше звірка може наздогнати ще живий у мережі виклик finalize/cancel"
)


class ReconcileStats:
    def __init__(self):
        self.checked = 0
        self.released = []  # [{type, reservation_id, operator_id, user_id, reserved_kwh/reserved_uah}, ...]
        self.races = 0      # release_reservation_hold() повернув False — паралельний StopTransaction устиг першим
        # Промпт 3c-ii: аномалії без безпечної автоматичної дії — лише алерт.
        self.alerts = []    # [{reservation_id, operator_id, invoice_id, reason}, ...]


async def _get_operator_token(operator_id: int):
    """
    Токен оператора для дзвінків у банк (uah-кроки 2/3). None (з логом),
    якщо не збережений чи не розшифровується — той виняток пропускається,
    не зупиняючи звірку решти (той самий підхід, що reconcile_operators.py:
    один зламаний токен не має валити прогін для інших операторів).
    """
    token_encrypted = await repo.get_operator_monobank_token_encrypted(operator_id)
    if not token_encrypted:
        logger.error("Звірка uah-резервацій: оператор %s без токена еквайрингу", operator_id)
        return None
    try:
        return decrypt_secret(token_encrypted)
    except (EncryptionKeyMissing, ValueError) as e:
        logger.error("Звірка uah-резервацій: не вдалося розшифрувати токен оператора %s: %s", operator_id, e)
        return None


# ---------------------------------------------------------------------------
# kWh (модель A) — без змін
# ---------------------------------------------------------------------------

async def _expire_stale(rows, item_type: str, stats: ReconcileStats):
    for row in rows:
        stats.checked += 1
        released = await repo.release_reservation_hold(row["operator_id"], row["id"], "expired")
        if released:
            stats.released.append({
                "type": item_type, "reservation_id": row["id"], "operator_id": row["operator_id"],
                "user_id": row["user_id"], "reserved_kwh": row["reserved_kwh"],
            })
        else:
            stats.races += 1


# ---------------------------------------------------------------------------
# uah (модель B, Промпт 3c-ii) — docs/plan-3c-ii.md, розділ 5
# ---------------------------------------------------------------------------

async def _reconcile_stale_awaiting_hold(older_than, stats: ReconcileStats):
    """Крок 1: 'awaiting_hold' застарілі."""
    rows = await repo.list_stale_awaiting_hold_reservations(older_than)
    for row in rows:
        stats.checked += 1
        token = await _get_operator_token(row["operator_id"])
        if token is None:
            continue
        try:
            invoice = await get_invoice_status(token, row["invoice_id"])
        except MonobankError as e:
            logger.error("Звірка: не вдалося перевірити hold-інвойс %s (резервація #%s): %s",
                         row["invoice_id"], row["id"], e)
            continue

        bank_status = (invoice.get("status") or "").strip()
        if bank_status == "hold":
            # АНОМАЛІЯ: банк підтвердив hold, а локально й досі
            # 'awaiting_hold' — вебхук не спрацював чи запізнився.
            # RemoteStart із ЦЬОГО процесу небезпечний (Інваріант процесу,
            # docs/plan-3c-ii.md) — лише алерт, рядок лишається недоторканим.
            stats.alerts.append({
                "reservation_id": row["id"], "operator_id": row["operator_id"],
                "invoice_id": row["invoice_id"],
                "reason": "банк підтвердив hold, а RemoteStart не відбувся — ручний розбір",
            })
            continue

        # Банк ніколи не тримав гроші (усе ще created/processing, або сам
        # інвойс уже expired/failure) — безпечно позначаємо локально.
        await repo.set_reservation_status(row["operator_id"], row["id"], "expired")
        stats.released.append({
            "type": "stale_awaiting_hold", "reservation_id": row["id"],
            "operator_id": row["operator_id"], "reserved_uah": row["reserved_uah"],
        })


async def _reconcile_stale_pending_uah(older_than, stats: ReconcileStats):
    """
    Крок 2 (гілка uah, другий блокер рев'ю — закрито): перед cancel_invoice()
    береться атомарна ЛОКАЛЬНА заявка (claim_reservation_for_settlement,
    expected_status='pending') — паралельний StartTransaction, що встиг
    активувати рядок, побачить заявку програною, і звірка НЕ подзвонить у
    банк удруге на вже (можливо) активну зарядку. Див. докстрінг модуля.
    """
    rows = await repo.list_stale_pending_uah_reservations(older_than)
    for row in rows:
        stats.checked += 1
        token = await _get_operator_token(row["operator_id"])
        if token is None:
            continue
        try:
            invoice = await get_invoice_status(token, row["invoice_id"])
        except MonobankError as e:
            logger.error("Звірка: не вдалося перевірити інвойс %s (резервація #%s, pending): %s",
                         row["invoice_id"], row["id"], e)
            continue

        bank_status = (invoice.get("status") or "").strip()
        if bank_status == "hold":
            claim = await repo.claim_reservation_for_settlement(
                row["operator_id"], row["id"], expected_status="pending",
            )
            if claim is None:
                logger.warning(
                    "Звірка: резервація #%s (pending-uah) вже не 'pending' — заявку "
                    "програно паралельному шляху (StartTransaction устиг активувати), "
                    "пропускаємо", row["id"],
                )
                stats.races += 1
                continue
            try:
                await cancel_invoice(token, row["invoice_id"])
            except MonobankError as e:
                logger.error("Звірка: не вдалося скасувати hold %s (резервація #%s, pending): %s",
                             row["invoice_id"], row["id"], e)
                continue
            await repo.record_uah_settlement(row["operator_id"], row["id"], "expired", Decimal("0"))
        elif bank_status == "success":
            final_amount = kopecks_to_uah(invoice.get("finalAmount", 0))
            await repo.record_uah_settlement(row["operator_id"], row["id"], "finalized", final_amount)
        else:
            # 'reversed'/'failure'/'expired' — банк уже сам дійшов до
            # фіналу (хтось інший устиг, чи водій так і не заплатив) —
            # синхронізуємо статус, БЕЗ повторного виклику банку.
            await repo.record_uah_settlement(row["operator_id"], row["id"], "expired", Decimal("0"))

        stats.released.append({
            "type": "stale_pending_uah", "reservation_id": row["id"],
            "operator_id": row["operator_id"], "reserved_uah": row["reserved_uah"],
        })


async def _reconcile_stale_active_uah(cutoff, stats: ReconcileStats):
    """
    Крок 3 (перший БЛОКЕР рев'ю бандла 3c-ii — закрито): 'active' uah-
    резервації, застряглі без StopTransaction. Другий блокер рев'ю (теж
    закрито): перед будь-яким finalize_invoice()/cancel_invoice() береться
    атомарна ЛОКАЛЬНА заявка (claim_reservation_for_settlement) — сам цей
    крок, доданий для закриття першого блокера, спершу дзвонив у банк без
    заявки; станція, що повертається з офлайну й шле накопичений
    StopTransaction, могла спричинити подвійний фіналайз паралельно з
    живим шляхом on_stop_transaction. Див. докстрінг модуля.
    """
    rows = await repo.list_stale_active_uah_reservations(cutoff)
    for row in rows:
        stats.checked += 1
        token = await _get_operator_token(row["operator_id"])
        if token is None:
            continue
        try:
            invoice = await get_invoice_status(token, row["invoice_id"])
        except MonobankError as e:
            logger.error("Звірка: не вдалося перевірити інвойс %s (резервація #%s, active-uah): %s",
                         row["invoice_id"], row["id"], e)
            continue

        bank_status = (invoice.get("status") or "").strip()
        if bank_status == "hold":
            claim = await repo.claim_reservation_for_settlement(row["operator_id"], row["id"])
            if claim is None:
                logger.warning(
                    "Звірка: резервація #%s (active-uah) вже не 'active' — заявку на "
                    "розрахунок програно паралельному шляху (ймовірно, on_stop_transaction "
                    "устиг першим), пропускаємо", row["id"],
                )
                stats.races += 1
                continue
            session = (
                await repo.get_session(row["operator_id"], row["operator_session_id"])
                if row["operator_session_id"] is not None else None
            )
            if session is not None and session["status"] == "completed":
                # Крах МІЖ complete_ocpp_transaction() і claim_reservation_
                # for_settlement() — факт спожитого відомий (сесія вже
                # 'completed'), рахуємо ним, тим самим шляхом, що й крок 4.
                station = await repo.get_station(row["operator_id"], row["station_id"])
                tariff_start = station["tariff_uah_start"] if station is not None else None
                tariff_kwh = station["tariff_uah_kwh"] if station is not None else Decimal("0")
                amount_uah = compute_uah_settlement_amount(
                    tariff_start, tariff_kwh, session["kwh"], row["reserved_uah"],
                )
                try:
                    if amount_uah > 0:
                        await finalize_invoice(token, row["invoice_id"], amount_uah)
                    else:
                        await cancel_invoice(token, row["invoice_id"])
                except MonobankError as e:
                    logger.error(
                        "Звірка: не вдалося довершити розрахунок %s (резервація #%s, active-uah): %s",
                        row["invoice_id"], row["id"], e,
                    )
                    continue
                await repo.record_uah_settlement(row["operator_id"], row["id"], "finalized", amount_uah)
                logger.error(
                    "Звірка: резервація #%s (оператор %s) застрягла 'active', хоча сесія вже "
                    "'completed' (крах між кроками settle) — розраховано автоматично на %s грн, "
                    "варто зʼясувати причину збою", row["id"], row["operator_id"], amount_uah,
                )
            else:
                # StopTransaction ніколи не прийшов — факт спожитого
                # невідомий, не вигадуємо: звільняємо ПОВНИЙ hold (той
                # самий принцип, що kWh-модель при kwh=None).
                try:
                    await cancel_invoice(token, row["invoice_id"])
                except MonobankError as e:
                    logger.error(
                        "Звірка: не вдалося скасувати застряглий hold %s (резервація #%s, active-uah): %s",
                        row["invoice_id"], row["id"], e,
                    )
                    continue
                await repo.record_uah_settlement(row["operator_id"], row["id"], "expired", Decimal("0"))
                logger.error(
                    "Звірка: резервація #%s (оператор %s) застрягла 'active' довше за --active-hours — "
                    "StopTransaction НІКОЛИ не прийшов, hold скасовано ПОВНІСТЮ (оператор НЕ отримав "
                    "оплату за фактично віддану енергію, якщо вона була) — перевір станцію",
                    row["id"], row["operator_id"],
                )
        elif bank_status == "success":
            final_amount = kopecks_to_uah(invoice.get("finalAmount", 0))
            await repo.record_uah_settlement(row["operator_id"], row["id"], "finalized", final_amount)
        else:
            await repo.record_uah_settlement(row["operator_id"], row["id"], "expired", Decimal("0"))

        stats.released.append({
            "type": "stale_active_uah", "reservation_id": row["id"],
            "operator_id": row["operator_id"], "reserved_uah": row["reserved_uah"],
        })


async def _reconcile_stale_settling(cutoff, stats: ReconcileStats):
    """Крок 4 (Промпт 3c-ii): 'settling' застарілі."""
    rows = await repo.list_stale_settling_reservations(cutoff)
    for row in rows:
        stats.checked += 1
        token = await _get_operator_token(row["operator_id"])
        if token is None:
            continue
        try:
            invoice = await get_invoice_status(token, row["invoice_id"])
        except MonobankError as e:
            logger.error("Звірка: не вдалося перевірити інвойс %s (резервація #%s, settling): %s",
                         row["invoice_id"], row["id"], e)
            continue

        bank_status = (invoice.get("status") or "").strip()
        if bank_status == "hold":
            # Ані finalize, ані cancel так і не дійшли до банку — самі
            # довершуємо, тим самим розрахунком, що й on_stop_transaction.
            session = (
                await repo.get_session(row["operator_id"], row["operator_session_id"])
                if row["operator_session_id"] is not None else None
            )
            station = await repo.get_station(row["operator_id"], row["station_id"])
            kwh = session["kwh"] if session is not None else None
            tariff_start = station["tariff_uah_start"] if station is not None else None
            tariff_kwh = station["tariff_uah_kwh"] if station is not None else Decimal("0")

            amount_uah = compute_uah_settlement_amount(tariff_start, tariff_kwh, kwh, row["reserved_uah"])
            try:
                if amount_uah > 0:
                    await finalize_invoice(token, row["invoice_id"], amount_uah)
                else:
                    await cancel_invoice(token, row["invoice_id"])
            except MonobankError as e:
                logger.error("Звірка: не вдалося довершити розрахунок %s (резервація #%s, settling): %s",
                             row["invoice_id"], row["id"], e)
                continue
            await repo.record_uah_settlement(row["operator_id"], row["id"], "finalized", amount_uah)
        elif bank_status == "success":
            # Банк уже фіналізував (наш дзвінок пройшов, а запис — ні) —
            # синхронізуємо статус його ж сумою, без повторного виклику.
            final_amount = kopecks_to_uah(invoice.get("finalAmount", 0))
            await repo.record_uah_settlement(row["operator_id"], row["id"], "finalized", final_amount)
        elif bank_status == "reversed":
            await repo.record_uah_settlement(row["operator_id"], row["id"], "finalized", Decimal("0"))
        else:
            # Неочікуваний стан для рядка, що вже мав дійти до hold —
            # гучний лог, не вигадуємо суму.
            logger.error(
                "Звірка: резервація #%s (settling) — банк у неочікуваному стані '%s', "
                "потрібен ручний розбір", row["id"], bank_status,
            )
            continue

        stats.released.append({
            "type": "stale_settling", "reservation_id": row["id"],
            "operator_id": row["operator_id"], "reserved_uah": row["reserved_uah"],
        })


def _format_line(item: dict) -> str:
    if item.get("reserved_uah") is not None:
        amount = f"{item['reserved_uah']} грн"
    else:
        amount = f"{item['reserved_kwh']} кВт·год"
    who = f", водій {item['user_id']}" if item.get("user_id") is not None else ""
    return (f"   [{item['type']}] резервація #{item['reservation_id']} "
            f"(оператор #{item['operator_id']}{who}) — звільнено/розраховано {amount}")


def _format_alert(item: dict) -> str:
    return (f"   резервація #{item['reservation_id']} (оператор #{item['operator_id']}, "
            f"інвойс {item['invoice_id']}) — {item['reason']}")


def _print_summary(stats: ReconcileStats):
    print(f"\n=== Звірка kWh/грн-резервацій ({datetime.now(timezone.utc).isoformat()}) ===\n")
    print(f"Перевірено пунктів: {stats.checked}")
    print(f"Звільнено/розраховано (протухли): {len(stats.released)}")
    if stats.released:
        for item in stats.released:
            print(_format_line(item))
    else:
        print("✅ Застряглих резервацій не знайдено.")
    if stats.races:
        print(f"\nℹ️ {stats.races} — паралельний StopTransaction устиг першим (не проблема).")
    if stats.alerts:
        print(f"\n⚠️ Потребують РУЧНОГО розбору: {len(stats.alerts)}")
        for item in stats.alerts:
            print(_format_alert(item))
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
    if not chat_id or not (stats.released or stats.alerts):
        return
    try:
        bot = _get_bot()
        lines = [
            "🔎 <b>Звірка kWh/грн-резервацій</b>",
            f"Перевірено: {stats.checked}",
            f"Звільнено/розраховано (протухли): {len(stats.released)}",
        ]
        for item in stats.released[:30]:
            lines.append(html.escape(_format_line(item).strip()))
        if len(stats.released) > 30:
            lines.append(f"… і ще {len(stats.released) - 30} пунктів (див. лог сервера)")
        if stats.alerts:
            lines.append(f"\n⚠️ Потребують РУЧНОГО розбору: {len(stats.alerts)}")
            for item in stats.alerts[:30]:
                lines.append(html.escape(_format_alert(item).strip()))
            if len(stats.alerts) > 30:
                lines.append(f"… і ще {len(stats.alerts) - 30} пунктів (див. лог сервера)")
        await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
    except Exception as e:
        # Збій пуша в Telegram не має ламати саму звірку — вона вже
        # завершилась і надрукувала підсумок у консоль.
        logger.error("Не вдалося надіслати підсумок звірки резервацій у LOGS_CHAT_ID: %s", e)


async def run(pending_minutes: int, active_hours: int,
              awaiting_hold_minutes: int = AWAITING_HOLD_STALE_MINUTES,
              now: datetime = None) -> int:
    """
    `now` — лише для тестів (детермінований "поточний момент" замість
    реального годинника); прод-запуск завжди йде без нього.

    SETTLING_STALE_SECONDS НЕ винесений у CLI-аргумент навмисно (на
    відміну від pending/active/awaiting-hold): це запобіжник, звʼязаний із
    monobank_acquiring.DEFAULT_TIMEOUT, а не бізнес-таймінг, який варто
    легко змінювати з командного рядка.
    """
    now = now or datetime.now(timezone.utc)
    pending_cutoff = now - timedelta(minutes=pending_minutes)
    active_cutoff = now - timedelta(hours=active_hours)
    awaiting_hold_cutoff = now - timedelta(minutes=awaiting_hold_minutes)
    settling_cutoff = now - timedelta(seconds=SETTLING_STALE_SECONDS)

    stats = ReconcileStats()
    await init_postgres()
    try:
        pending_rows = await repo.list_stale_pending_reservations(pending_cutoff)
        active_rows = await repo.list_stale_active_reservations(active_cutoff)
        await _expire_stale(pending_rows, "stale_pending", stats)
        await _expire_stale(active_rows, "stale_active", stats)

        await _reconcile_stale_awaiting_hold(awaiting_hold_cutoff, stats)
        await _reconcile_stale_pending_uah(pending_cutoff, stats)
        await _reconcile_stale_active_uah(active_cutoff, stats)
        await _reconcile_stale_settling(settling_cutoff, stats)
    finally:
        await close_postgres()

    _print_summary(stats)
    await _push_summary_to_telegram(stats)

    return 1 if (stats.released or stats.alerts) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Звірка kWh/грн-резервацій (Промпти 3c-i/3c-ii).")
    parser.add_argument("--pending-minutes", type=int, default=PENDING_STALE_MINUTES,
                        help=f"Поріг для 'pending' резервацій, у хвилинах "
                             f"(за замовчуванням {PENDING_STALE_MINUTES}).")
    parser.add_argument("--active-hours", type=int, default=ACTIVE_STALE_HOURS,
                        help=f"Поріг для 'active' резервацій, у годинах "
                             f"(за замовчуванням {ACTIVE_STALE_HOURS}).")
    parser.add_argument("--awaiting-hold-minutes", type=int, default=AWAITING_HOLD_STALE_MINUTES,
                        help=f"Поріг для 'awaiting_hold' (модель B) резервацій, у хвилинах "
                             f"(за замовчуванням {AWAITING_HOLD_STALE_MINUTES}).")
    args = parser.parse_args()

    exit_code = asyncio.run(run(args.pending_minutes, args.active_hours, args.awaiting_hold_minutes))
    raise SystemExit(exit_code)
