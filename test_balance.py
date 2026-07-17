"""
Тести на app/database/connection.py::update_user_balance — єдину точку
запису балансу. Ключове правило, яке тут перевіряється: депозит завжди
пишеться в kw_transactions додатним числом, списання — від'ємним, і
users.balance оновлюється в той самий бік. До фіксу три різні модулі
рахували баланс по-різному (SUM(kw_transactions.amount) в одному місці,
пряме читання users.balance в іншому), і знак розходився.

Тести не піднімають реальну Postgres — підміняють з'єднання фейковим
об'єктом, що просто записує, які SQL-запити й параметри були виконані.

Запуск: pytest test_balance.py -v
"""
from app.database.connection import update_user_balance


class FakeConnection:
    """Мінімальна заглушка asyncpg.Connection: лише запам'ятовує виклики execute()."""

    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return "OK"


def _find_call(fake_conn, needle):
    """Повертає (query, args) першого запису, де needle зустрічається в SQL."""
    for query, args in fake_conn.calls:
        if needle in query:
            return query, args
    raise AssertionError(f"Жоден execute() не містив '{needle}'. Виклики: {fake_conn.calls}")


async def test_deposit_increases_balance_and_stores_positive_amount():
    conn = FakeConnection()
    await update_user_balance(
        user_id=123, amount_kwh=10.0, t_type="deposit", conn=conn,
    )

    balance_query, balance_args = _find_call(conn, "UPDATE users SET balance = balance +")
    assert balance_args == (10.0, 123)

    ledger_query, ledger_args = _find_call(conn, "INSERT INTO kw_transactions")
    # тип тепер передається параметром (з явним ::transaction_type кастом),
    # а не літералом у тексті запиту — перевіряємо значення параметра.
    # Параметри INSERT: (user_id, type, amount, payment_id, session_id, description)
    assert "$2::transaction_type" in ledger_query
    assert ledger_args[1] == "deposit"
    assert ledger_args[2] == 10.0  # додатне значення для поповнення


async def test_withdrawal_decreases_balance_and_stores_negative_amount():
    conn = FakeConnection()
    await update_user_balance(
        user_id=456, amount_kwh=7.5, t_type="ocpi_session", conn=conn,
    )

    balance_query, balance_args = _find_call(conn, "UPDATE users SET balance = balance -")
    assert balance_args == (7.5, 456)

    ledger_query, ledger_args = _find_call(conn, "INSERT INTO kw_transactions")
    assert "'withdrawal'" in ledger_query
    assert ledger_args[1] == -7.5  # від'ємне значення для списання — ключова умова балансу


async def test_monobank_jar_deposit_treated_as_deposit():
    """t_type='monobank_jar' теж має рахуватись як поповнення (додатний знак)."""
    conn = FakeConnection()
    await update_user_balance(
        user_id=789, amount_kwh=3.0, t_type="monobank_jar", conn=conn,
    )
    ledger_query, ledger_args = _find_call(conn, "INSERT INTO kw_transactions")
    assert "$2::transaction_type" in ledger_query
    assert ledger_args[1] == "deposit"
    assert ledger_args[2] == 3.0


async def test_refund_increases_balance_and_stores_refund_type_not_deposit():
    """t_type='refund' — кредит (як депозит), але в журналі має бути тип
    'refund', а не 'deposit', щоб компенсації відрізнялись від поповнень."""
    conn = FakeConnection()
    await update_user_balance(
        user_id=321, amount_kwh=5.0, t_type="refund", conn=conn,
    )

    balance_query, balance_args = _find_call(conn, "UPDATE users SET balance = balance +")
    assert balance_args == (5.0, 321)

    ledger_query, ledger_args = _find_call(conn, "INSERT INTO kw_transactions")
    assert "$2::transaction_type" in ledger_query
    assert ledger_args[1] == "refund"  # тип у журналі — саме 'refund'
    assert ledger_args[2] == 5.0  # додатне значення, як і депозит


async def test_balance_and_ledger_share_the_same_transaction_connection():
    """Обидва запити (users.balance і kw_transactions) виконуються через
    один і той самий переданий conn — це і є гарантія атомарності, коли
    виклик іде всередині чужої транзакції (наприклад, разом з записом CDR)."""
    conn = FakeConnection()
    await update_user_balance(user_id=1, amount_kwh=1.0, t_type="deposit", conn=conn)
    # upsert user + update balance + insert ledger = мінімум 3 виклики execute на цьому ж conn
    assert len(conn.calls) >= 3
