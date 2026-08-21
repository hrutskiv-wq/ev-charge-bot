"""
Ремонт осиротілого балансу: створити відсутній рядок `users` для
користувача, на якого вже посилається журнал `kw_transactions`.

    python scripts/repair_orphan_balance.py                      # перелік, нічого не міняти
    python scripts/repair_orphan_balance.py 8855895437           # dry-run по одному
    python scripts/repair_orphan_balance.py 8855895437 --apply
    python scripts/repair_orphan_balance.py 8855895437 --apply --zero-out "причина"

## Навіщо окремий скрипт

Штатного способу привести кеш до наявного журналу не існує:

  * `get_user_data()` створює рядок із `balance = 0.00`. Журнал каже +50 —
    інваріант лишається зламаним, лише інакше;
  * `update_user_balance()` сам по собі допише ЩЕ ОДИН рядок у журнал:
    сума стане +100 при балансі 50. Гірше, ніж було.

Тому баланс береться з журналу прямою вставкою, а далі — за потреби —
коригується ШТАТНО, через `update_user_balance(t_type="correction")`.

## Два режими, і різниця між ними коштує реальних кВт·год

**Дефолт — створити рахунок із сумою журналу.** Для справжнього осиротілого
користувача це єдине правильне: журнал каже, що йому належить N кВт·год,
і рахунок має це відображати.

**`--zero-out "причина"` — додатково занулити коригувальним рядком.**
Вмикається ЯВНО й лише тоді, коли нарахування було помилковим по суті.
Дефолтом бути не може: мовчазне занулення знищило б реальні кВт·год
справжнього користувача.

Обидві дії йдуть в ОДНІЙ транзакції, тому проміжний баланс (сума журналу
до коригування) ззовні не спостерігається.

## Журнал

Не редагується й не видаляється **ніколи**. `--zero-out` не прибирає
початковий рядок, а ДОПИСУЄ коригувальний — append-only лишається
append-only, а слід помилки лишається видимим назавжди.

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

# LEFT JOIN, не JOIN — з тієї самої причини, що в канонічному запиті
# інваріанта (`CLAUDE.md` §6b): внутрішнє з'єднання не бачить саме тих
# рядків, які шукаємо.
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


async def _list_orphans(conn, user_id=None):
    rows = await conn.fetch(ORPHANS_SQL)
    if user_id is not None:
        rows = [r for r in rows if r["user_id"] == user_id]
    return rows


async def _repair_one(conn, user_id: int, zero_out: str | None) -> None:
    """Один осиротілий користувач, одна транзакція."""
    async with conn.transaction():
        # Баланс беремо з журналу тим самим запитом, у тій самій транзакції —
        # щоб між читанням і вставкою не з'явився новий рядок журналу й
        # значення не розійшлись.
        tag = await conn.execute(
            """
            INSERT INTO users (user_id, balance)
            SELECT $1, COALESCE(SUM(amount), 0)
            FROM kw_transactions
            WHERE user_id = $1
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id,
        )
        if connection._rows_affected(tag) == 0:
            print(f"  user_id={user_id}: рядок уже існує — пропущено")
            return

        created = await conn.fetchval(
            "SELECT balance FROM users WHERE user_id = $1", user_id
        )
        print(f"  user_id={user_id}: рахунок створено, balance={created}")

        if zero_out:
            # Штатний шлях: гілка correction зі ЗНАКОВОЮ сумою, у ТІЙ САМІЙ
            # транзакції (conn передається). Проміжний баланс ззовні не
            # спостерігається.
            await connection.update_user_balance(
                user_id=user_id,
                amount_kwh=-float(created),
                t_type="correction",
                conn=conn,
                description=zero_out,
            )
            print(f"  user_id={user_id}: коригування {-float(created)} записано")

        balance = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id)
        ledger = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM kw_transactions WHERE user_id = $1", user_id
        )
        ok = abs(float(balance) - float(ledger)) < 0.0001
        print(
            f"  user_id={user_id}: balance={balance}, сума журналу={ledger} — "
            f"{'OK' if ok else 'РОЗБІЖНІСТЬ'}"
        )
        if not ok:
            raise RuntimeError(
                f"інваріант не зійшовся для user_id={user_id} — транзакцію відкочено"
            )


async def repair(user_id: int | None, apply: bool, zero_out: str | None) -> int:
    if zero_out and user_id is None:
        print("--zero-out вимагає конкретного user_id: занулювати всіх підряд не можна.")
        return 2

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
                mode = "створити рахунок + занулити коригуванням" if zero_out else "створити рахунок із сумою журналу"
                print(f"\nРежим: {mode}")
                print("DRY-RUN: нічого не змінено. Для запису додайте --apply.")
                return 0

            for row in orphans:
                await _repair_one(conn, row["user_id"], zero_out)

            remaining = await _list_orphans(conn, user_id)
            print(f"\nЗалишилось осиротілих: {len(remaining)}")
            return 0 if not remaining else 1
    finally:
        await connection.close_postgres()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ремонт осиротілого балансу: створити відсутній рядок users.",
    )
    parser.add_argument(
        "user_id", nargs="?", type=int,
        help="полагодити лише цього; без нього — лише перелік",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="справді записати (без нього — dry-run)",
    )
    parser.add_argument(
        "--zero-out", metavar="ПРИЧИНА", default=None,
        help=(
            "додатково занулити баланс коригувальним рядком із цим описом. "
            "ЛИШЕ коли нарахування помилкове по суті — для справжнього "
            "користувача це знищить реальні кВт·год"
        ),
    )
    args = parser.parse_args()

    sys.exit(asyncio.run(repair(args.user_id, args.apply, args.zero_out)))
