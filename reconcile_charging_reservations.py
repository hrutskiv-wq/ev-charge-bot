"""
Звірка kWh-резервацій (Промпт 3c-i, модель A).

Добирає резервації, чий OCPP-цикл "застряг" і НЕ дійшов до StopTransaction
(complete_ocpp_transaction_and_release() тому й не спрацював — атомарність
Промпту 3c-i покриває лише шлях від StartTransaction до StopTransaction,
не випадок, коли станція взагалі не відповіла чи сесія обірвалась ще ДО
StopTransaction):

  1. 'pending' резервації старші за --pending-minutes — RemoteStart ніколи
     не підтвердився StartTransaction (станція не відповіла/відхилила/
     втратила звʼязок одразу після create_charging_reservation). hold
     стоїть, водій не бачить свій кВт·год.
  2. 'active' резервації, чия сесія (за updated_at) не закривалась
     StopTransaction довше за --active-hours — станція перепідключилась
     і "забула" transactionId, або аварійно вимкнулась.

Обидва випадки — звільняємо ПОВНИЙ hold назад на баланс водія і позначаємо
резервацію 'expired' через release_reservation_hold() (єдина атомарна
UPDATE ... RETURNING точка, той самий виклик, що й start_charging_session.py
використовує для скасування). Race з паралельним StopTransaction безпечна —
0 affected rows, ніякого подвійного звільнення.

Використання:
    python reconcile_charging_reservations.py [--pending-minutes N] [--active-hours N]
    docker compose exec bot python reconcile_charging_reservations.py

Код виходу: 0 — застряглих резервацій не знайдено; 1 — знайдено й
автоматично звільнено (не помилка сама по собі, але сигнал моніторингу, що
десь застрягла OCPP-сесія — варто зазирнути в лог станції). Той самий
контракт, що й reconcile_operators.py/reconcile_payments.py (зручно для
cron).
"""
import argparse
import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

from app.database import operators_repo as repo
from app.database.connection import close_postgres, init_postgres

logger = logging.getLogger(__name__)

# RemoteStart -> StartTransaction — станція має відповісти майже миттєво;
# 30 хв — щедрий запас на затримки/ретраї, перш ніж вважати hold застряглим.
PENDING_STALE_MINUTES = 30
# Реальна зарядка може тривати кілька годин, тому поріг вищий, ніж для
# pending — 24 год явно за межею типової сесії.
ACTIVE_STALE_HOURS = 24


class ReconcileStats:
    def __init__(self):
        self.checked = 0
        self.released = []  # [{type, reservation_id, operator_id, user_id, reserved_kwh}, ...]
        self.races = 0      # release_reservation_hold() повернув False — паралельний StopTransaction устиг першим


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


def _format_line(item: dict) -> str:
    return (f"   [{item['type']}] резервація #{item['reservation_id']} "
            f"(оператор #{item['operator_id']}, водій {item['user_id']}) — "
            f"звільнено {item['reserved_kwh']} кВт·год")


def _print_summary(stats: ReconcileStats):
    print(f"\n=== Звірка kWh-резервацій ({datetime.now(timezone.utc).isoformat()}) ===\n")
    print(f"Перевірено пунктів: {stats.checked}")
    print(f"Звільнено (протухли): {len(stats.released)}")
    if stats.released:
        for item in stats.released:
            print(_format_line(item))
    else:
        print("✅ Застряглих резервацій не знайдено.")
    if stats.races:
        print(f"\nℹ️ {stats.races} — паралельний StopTransaction устиг першим (не проблема).")
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
    if not chat_id or not stats.released:
        return
    try:
        bot = _get_bot()
        lines = [
            "🔎 <b>Звірка kWh-резервацій</b>",
            f"Перевірено: {stats.checked}",
            f"Звільнено (протухли): {len(stats.released)}",
        ]
        for item in stats.released[:30]:
            lines.append(html.escape(_format_line(item).strip()))
        if len(stats.released) > 30:
            lines.append(f"… і ще {len(stats.released) - 30} пунктів (див. лог сервера)")
        await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
    except Exception as e:
        # Збій пуша в Telegram не має ламати саму звірку — вона вже
        # завершилась і надрукувала підсумок у консоль.
        logger.error("Не вдалося надіслати підсумок звірки резервацій у LOGS_CHAT_ID: %s", e)


async def run(pending_minutes: int, active_hours: int, now: datetime = None) -> int:
    """
    `now` — лише для тестів (детермінований "поточний момент" замість
    реального годинника); прод-запуск завжди йде без нього.
    """
    now = now or datetime.now(timezone.utc)
    pending_cutoff = now - timedelta(minutes=pending_minutes)
    active_cutoff = now - timedelta(hours=active_hours)

    stats = ReconcileStats()
    await init_postgres()
    try:
        pending_rows = await repo.list_stale_pending_reservations(pending_cutoff)
        active_rows = await repo.list_stale_active_reservations(active_cutoff)
        await _expire_stale(pending_rows, "stale_pending", stats)
        await _expire_stale(active_rows, "stale_active", stats)
    finally:
        await close_postgres()

    _print_summary(stats)
    await _push_summary_to_telegram(stats)

    return 1 if stats.released else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Звірка kWh-резервацій (Промпт 3c-i).")
    parser.add_argument("--pending-minutes", type=int, default=PENDING_STALE_MINUTES,
                        help=f"Поріг для 'pending' резервацій, у хвилинах "
                             f"(за замовчуванням {PENDING_STALE_MINUTES}).")
    parser.add_argument("--active-hours", type=int, default=ACTIVE_STALE_HOURS,
                        help=f"Поріг для 'active' резервацій, у годинах "
                             f"(за замовчуванням {ACTIVE_STALE_HOURS}).")
    args = parser.parse_args()

    exit_code = asyncio.run(run(args.pending_minutes, args.active_hours))
    raise SystemExit(exit_code)
