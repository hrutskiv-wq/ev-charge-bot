"""
Реконсиляція платежів: звіряє таблицю `payments` (наші записи про кожен
успішний платіж — Monobank webhook, Telegram invoice) з журналом
kw_transactions (реальне нарахування кВт·год). Ловить саме той клас багів,
який уже неодноразово траплявся в цьому проєкті:

  - платіж позначено 'success' у payments, але відповідного нарахування
    в kw_transactions немає (гроші взяли, кВт·год не дали) — саме так
    поводився старий Monobank-webhook до фіксу update_user_balance();
  - є нарахування (kw_transactions.payment_id вказує на платіж), але
    платежу з таким id немає або він не 'success' (нарахування без
    підтвердженого платежу — потенційна накрутка або баг);
  - сума нарахованих кВт·год не відповідає сплаченій сумі за тарифом
    (з урахуванням двох фіксованих пакетів "750 грн -> 50 кВт·год" і
    "1350 грн -> 100 кВт·год зі знижкою" — це НЕ прямий поділ на
    PRICE_PER_KWH, тому звіряємо саме за тією ж логікою, що й у
    app/api/payments.py::monobank_webhook і
    app/handlers/user.py::process_successful_payment).

Використання:
    python reconcile_payments.py [--days N]   # за замовчуванням 7 днів

Код виходу: 0 — усе збігається; 1 — знайдено розходження (зручно для cron +
моніторингу: ненульовий exit code -> алерт).
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from app.database.connection import init_postgres, close_postgres, get_db_pool, PRICE_PER_KWH

# Допуск на округлення (kwh_to_add для нетипових сум округлюється до 2 знаків).
AMOUNT_TOLERANCE_KWH = 0.05


def _expected_kwh_for_payment(amount_uah) -> float:
    """Відтворює ту саму бізнес-логіку нарахування, що й у монобанк-вебхуку
    та телеграм-інвойсах: два фіксовані пакети зі знижкою, інакше —
    пропорційно PRICE_PER_KWH."""
    raw_kopecks = round(float(amount_uah) * 100)
    if raw_kopecks == 75000:
        return 50.0
    if raw_kopecks == 135000:
        return 100.0
    return round(float(amount_uah) / PRICE_PER_KWH, 2)


async def find_paid_but_not_credited(pool, since):
    query = """
        SELECT p.id, p.user_id, p.invoice_id, p.provider, p.amount, p.created_at
        FROM payments p
        LEFT JOIN kw_transactions kt ON kt.payment_id = p.id AND kt.type = 'deposit'
        WHERE p.status = 'success'
          AND p.created_at >= $1
          AND kt.id IS NULL
        ORDER BY p.created_at
    """
    async with pool.acquire() as conn:
        return await conn.fetch(query, since)


async def find_credited_without_valid_payment(pool, since):
    query = """
        SELECT kt.id, kt.user_id, kt.payment_id, kt.amount, kt.created_at
        FROM kw_transactions kt
        LEFT JOIN payments p ON p.id = kt.payment_id
        WHERE kt.type = 'deposit'
          AND kt.payment_id IS NOT NULL
          AND kt.created_at >= $1
          AND (p.id IS NULL OR p.status != 'success')
        ORDER BY kt.created_at
    """
    async with pool.acquire() as conn:
        return await conn.fetch(query, since)


async def find_amount_mismatches(pool, since):
    query = """
        SELECT p.id AS payment_id, p.user_id, p.invoice_id, p.amount AS paid_uah,
               kt.id AS tx_id, kt.amount AS credited_kwh
        FROM payments p
        JOIN kw_transactions kt ON kt.payment_id = p.id AND kt.type = 'deposit'
        WHERE p.status = 'success'
          AND p.created_at >= $1
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, since)

    mismatches = []
    for row in rows:
        expected_kwh = _expected_kwh_for_payment(row["paid_uah"])
        actual_kwh = float(row["credited_kwh"])
        if abs(expected_kwh - actual_kwh) > AMOUNT_TOLERANCE_KWH:
            mismatches.append((row, expected_kwh, actual_kwh))
    return mismatches


async def run(days: int) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    await init_postgres()
    pool = await get_db_pool()

    try:
        paid_not_credited = await find_paid_but_not_credited(pool, since)
        credited_without_payment = await find_credited_without_valid_payment(pool, since)
        amount_mismatches = await find_amount_mismatches(pool, since)
    finally:
        await close_postgres()

    print(f"\n=== Реконсиляція платежів за останні {days} дн. (з {since.isoformat()}) ===\n")

    problems_found = False

    if paid_not_credited:
        problems_found = True
        print(f"❌ ОПЛАЧЕНО, АЛЕ НЕ НАРАХОВАНО ({len(paid_not_credited)}):")
        for r in paid_not_credited:
            print(f"   payment_id={r['id']} user={r['user_id']} invoice={r['invoice_id']} "
                  f"provider={r['provider']} amount={r['amount']} грн created_at={r['created_at']}")
    else:
        print("✅ Усі успішні платежі мають відповідне нарахування кВт·год.")

    if credited_without_payment:
        problems_found = True
        print(f"\n❌ НАРАХОВАНО БЕЗ ПІДТВЕРДЖЕНОГО ПЛАТЕЖУ ({len(credited_without_payment)}):")
        for r in credited_without_payment:
            print(f"   tx_id={r['id']} user={r['user_id']} payment_id={r['payment_id']} "
                  f"amount={r['amount']} кВт·год created_at={r['created_at']}")
    else:
        print("✅ Усі нарахування депозитів прив'язані до підтвердженого платежу.")

    if amount_mismatches:
        problems_found = True
        print(f"\n❌ РОЗБІЖНІСТЬ СУМИ ({len(amount_mismatches)}):")
        for row, expected_kwh, actual_kwh in amount_mismatches:
            print(f"   payment_id={row['payment_id']} user={row['user_id']} invoice={row['invoice_id']} "
                  f"сплачено={row['paid_uah']} грн (очікується ~{expected_kwh:.2f} кВт·год), "
                  f"нараховано={actual_kwh:.2f} кВт·год")
    else:
        print("✅ Суми оплат і нарахувань відповідають тарифу (з допуском округлення).")

    print()
    return 1 if problems_found else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Реконсиляція платежів kw_transactions vs payments.")
    parser.add_argument("--days", type=int, default=7, help="Період перевірки в днях (за замовчуванням 7).")
    args = parser.parse_args()

    exit_code = asyncio.run(run(args.days))
    raise SystemExit(exit_code)
