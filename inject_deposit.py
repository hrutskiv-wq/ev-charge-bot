"""
Ручний адмінський скрипт для нарахування кВт·год конкретному користувачу
(наприклад, компенсація або тестове поповнення). Використання:

    python inject_deposit.py <user_id> <amount_kwh> ["опис"]

Раніше скрипт мав синтаксичну помилку (незакавичені літерали `deposit` і
`Тестове` як аргументи) і не міг виконатись взагалі, а також писав
напряму в kw_transactions в обхід update_user_balance(), тому не оновлював
users.balance. Тепер він іде через єдину точку запису балансу.
"""
import asyncio
import sys

from app.database import connection


async def add_deposit(user_id: int, amount_kwh: float, description: str = "Ручне поповнення (адмін-скрипт)"):
    await connection.init_postgres()
    await connection.update_user_balance(
        user_id=user_id,
        amount_kwh=amount_kwh,
        t_type="deposit",
        description=description,
    )
    await connection.close_postgres()
    print(f"✅ Нараховано {amount_kwh} кВт·год користувачу {user_id}.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Використання: python inject_deposit.py <user_id> <amount_kwh> [\"опис\"]")
        sys.exit(1)

    arg_user_id = int(sys.argv[1])
    arg_amount = float(sys.argv[2])
    arg_description = sys.argv[3] if len(sys.argv) > 3 else "Ручне поповнення (адмін-скрипт)"

    asyncio.run(add_deposit(arg_user_id, arg_amount, arg_description))
