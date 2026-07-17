"""
Ручний адмінський скрипт для повернення (рефанду) кВт·год користувачу —
наприклад, коли CDR від CPO прийшов і кВт·год списались з балансу, а
фізична сесія зарядки насправді не відбулась (обірваний кабель, збій
станції тощо). Використання:

    python refund_transaction.py <user_id> <amount_kwh> ["опис"]

На відміну від inject_deposit.py (звичайне поповнення), ця операція
записується в журнал kw_transactions окремим типом 'refund', а не
'deposit' — щоб компенсації було видно окремо від звичайних поповнень
при звірці/реконсиляції платежів.

Автоматичного визначення "коли саме потрібен рефанд" немає навмисно:
CDR сам по собі не сигналізує однозначно про невдалу фізичну сесію,
тому рішення про рефанд — завжди дія людини (підтримки/адміна) у
відповідь на звернення користувача, а не автоматичний тригер.
"""
import asyncio
import sys

from app.database import connection


async def refund(user_id: int, amount_kwh: float, description: str = "Повернення коштів (компенсація)"):
    await connection.init_postgres()
    await connection.update_user_balance(
        user_id=user_id,
        amount_kwh=amount_kwh,
        t_type="refund",
        description=description,
    )
    await connection.close_postgres()
    print(f"✅ Повернено {amount_kwh} кВт·год користувачу {user_id} (тип: refund).")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print('Використання: python refund_transaction.py <user_id> <amount_kwh> ["опис"]')
        sys.exit(1)

    arg_user_id = int(sys.argv[1])
    arg_amount = float(sys.argv[2])
    arg_description = sys.argv[3] if len(sys.argv) > 3 else "Повернення коштів (компенсація)"

    asyncio.run(refund(arg_user_id, arg_amount, arg_description))
