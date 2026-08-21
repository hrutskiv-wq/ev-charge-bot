"""
Ремонт осиротілого балансу: створити відсутній рядок `users` для
користувача, на якого вже посилається журнал `kw_transactions`.

    python scripts/repair_orphan_balance.py                 # знайти всіх, нічого не міняти
    python scripts/repair_orphan_balance.py 8855895437      # dry-run по одному
    python scripts/repair_orphan_balance.py 8855895437 --apply

## Навіщо окремий скрипт

Штатного способу зробити це не існує, і саме тому потрібен скрипт, а не
виклик наявної функції:

  * `get_user_data()` створює рядок із `balance = 0.00`. Журнал каже +50 —
    інваріант лишається зламаним, лише інакше;
  * `update_user_balance()` допише ЩЕ ОДИН рядок у журнал: сума стане +100
    при балансі 50. Гірше, ніж було;
  * гілки `correction` у ній немає взагалі — `correction` потрапив би у
    фінальний `else` і СПИСАВСЯ б. Спостерігалось 06.08.2026, задокументовано
    в `CLAUDE.md` §6b.

## Що робить і чого не робить

Рахує баланс як `SUM(kw_transactions.amount)` по цьому користувачу і
створює рядок `users` з цим значенням. **Журналу не торкається взагалі** —
ні INSERT, ні UPDATE, ні DELETE.

Коригувальний рядок навмисно НЕ додається. Журнал append-only і є джерелом
правди; рядок +50 не був помилковим — була відсутня похідна від нього.
Занулення означало б дописати `correction −50`, тобто стверджувати, що
відбулась компенсація, якої не було. Розбір і обґрунтування —
`docs/plan-orphan-balance-guard.md` §2.3.

## Порядок

Виконати ДО міграції 0020 (FK `kw_transactions.user_id -> users`). З
осиротілим рядком `ADD CONSTRAINT` впаде на перевірці наявних даних —
у цьому й сенс жорсткої послідовності.

Дефолт — dry-run. Запис лише з `--apply`.
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Скрипт лежить у scripts/, а не в корені, як старі разові скрипти
# (refund_transaction.py тощо — вони в корені, і це окремий пункт боргу,
# PROJECT_CONTEXT.md §12). Тому sys.path[0] — це scripts/, і `app` без
# цього рядка не імпортується.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import connection  # noqa: E402

ORPHANS_SQL = """
    SELECT t.user_id,
           SUM(t.amount) AS ledger_sum,
           count(*)      AS rows_count
    FROM kw_transactions t
    LEFT JOIN users u ON u.user_id = t.user_id
    WHERE u.user_id IS NULL
    GROUP BY t.user_id
    ORDER BY t.user_id
"""

# LEFT JOIN, не JOIN — з тієї самої причини, що в канонічному запиті
# інваріанта (`CLAUDE.md` §6b): внутрішнє з'єднання не бачить саме тих
# рядків, які шукаємо.


async def _list_orphans(conn, user_id=None):
    rows = await conn.fetch(ORPHANS_SQL)
    if user_id is not None:
        rows = [r for r in rows if r["user_id"] == user_id]
    return rows


async def repair(user_id: int | None, apply: bool) -> int:
    await connection.init_postgres()
    pool = await connection.get_db_pool()
    try:
        async with pool.acquire() as conn:
            orphans = await _list_orphans(conn, user_id)

            if not orphans:
                target = f"user_id={user_id}" if user_id is not None else "жодного"
                print(f"Осиротілих рядків не знайдено ({target}).")
                return 0

            print(f"Знайдено осиротілих користувачів: {len(orphans)}")
            for row in orphans:
                print(
                    f"  user_id={row['user_id']}  рядків журналу={row['rows_count']}  "
                    f"сума={row['ledger_sum']} кВт·год"
                )

            if not apply:
                print("\nDRY-RUN: нічого не змінено. Для запису додайте --apply.")
                return 0

            for row in orphans:
                uid = row["user_id"]
                async with conn.transaction():
                    # Баланс береться з журналу тим самим запитом, в одній
                    # транзакції — щоб між читанням і вставкою не з'явився
                    # новий рядок журналу й не розійшлись значення.
                    tag = await conn.execute(
                        """
                        INSERT INTO users (user_id, balance)
                        SELECT $1, COALESCE(SUM(amount), 0)
                        FROM kw_transactions
                        WHERE user_id = $1
                        ON CONFLICT (user_id) DO NOTHING
                        """,
                        uid,
                    )
                    if connection._rows_affected(tag) == 0:
                        print(f"  user_id={uid}: рядок уже існує — пропущено")
                        continue

                    balance = await conn.fetchval(
                        "SELECT balance FROM users WHERE user_id = $1", uid
                    )
                    ledger = await conn.fetchval(
                        "SELECT COALESCE(SUM(amount), 0) FROM kw_transactions WHERE user_id = $1",
                        uid,
                    )
                    ok = abs(float(balance) - float(ledger)) < 0.0001
                    mark = "OK" if ok else "РОЗБІЖНІСТЬ"
                    print(
                        f"  user_id={uid}: створено, balance={balance}, "
                        f"сума журналу={ledger} — {mark}"
                    )
                    if not ok:
                        raise RuntimeError(
                            f"інваріант не зійшовся для user_id={uid} — транзакцію відкочено"
                        )

            remaining = await _list_orphans(conn, user_id)
            print(f"\nЗалишилось осиротілих: {len(remaining)}")
            return 0 if not remaining else 1
    finally:
        await connection.close_postgres()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("user_id", nargs="?", type=int, help="полагодити лише цього; без нього — лише перелік")
    parser.add_argument("--apply", action="store_true", help="справді записати (без нього — dry-run)")
    args = parser.parse_args()

    sys.exit(asyncio.run(repair(args.user_id, args.apply)))
