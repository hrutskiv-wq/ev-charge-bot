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


# ---------------------------------------------------------------------------
# hold / release (Промпт 3c-i — kWh-резервація на OCPP-сесію)
# ---------------------------------------------------------------------------

class FakeConnectionWithBalance(FakeConnection):
    """
    Розширює FakeConnection реальною семантикою `UPDATE ... WHERE
    balance >= $1` — потрібно, щоб перевірити саме той запобіжник, що
    відрізняє 'hold' від решти гілок списання.
    """

    def __init__(self, current_balance):
        super().__init__()
        self.balance = current_balance

    async def execute(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        if "balance >= $1" in query:
            amount_kwh = args[0]
            if self.balance >= amount_kwh:
                self.balance -= amount_kwh
                return "UPDATE 1"
            return "UPDATE 0"
        return "OK"


async def test_hold_decreases_balance_and_stores_hold_type_when_sufficient():
    conn = FakeConnectionWithBalance(current_balance=50.0)
    result = await update_user_balance(
        user_id=1, amount_kwh=20.0, t_type="hold", conn=conn,
        session_id="reservation-7",
    )

    assert result is True
    balance_query, balance_args = _find_call(conn, "balance >= $1")
    assert balance_args == (20.0, 1)

    ledger_query, ledger_args = _find_call(conn, "INSERT INTO kw_transactions")
    assert "$2::transaction_type" in ledger_query
    assert ledger_args[1] == "hold"
    assert ledger_args[2] == -20.0  # від'ємне — це списання, як і withdrawal
    assert ledger_args[4] == "reservation-7"  # session_id — звʼязок з резервацією


async def test_hold_fails_without_writing_anything_when_balance_insufficient():
    """
    Найважливіший тест гарантії "не спожити той самий баланс двічі":
    якщо балансу не вистачає, hold НЕ проводиться — ні users.balance, ні
    kw_transactions не чіпаються, функція повертає False.
    """
    conn = FakeConnectionWithBalance(current_balance=5.0)
    result = await update_user_balance(
        user_id=1, amount_kwh=20.0, t_type="hold", conn=conn,
    )

    assert result is False
    assert conn.balance == 5.0, "Баланс не мав змінитись при відмові"
    ledger_calls = [c for c in conn.calls if "INSERT INTO kw_transactions" in c[0]]
    assert ledger_calls == [], "Жодного ledger-рядка не мало з'явитись при відмові"


async def test_release_increases_balance_and_stores_release_type_not_deposit():
    conn = FakeConnection()
    result = await update_user_balance(
        user_id=1, amount_kwh=8.0, t_type="release", conn=conn,
        session_id="reservation-7",
    )

    assert result is True
    balance_query, balance_args = _find_call(conn, "UPDATE users SET balance = balance +")
    assert balance_args == (8.0, 1)

    ledger_query, ledger_args = _find_call(conn, "INSERT INTO kw_transactions")
    assert "$2::transaction_type" in ledger_query
    assert ledger_args[1] == "release"
    assert ledger_args[2] == 8.0  # додатне — це кредит
    assert ledger_args[4] == "reservation-7"


async def test_regular_debits_still_always_return_true():
    """Гілки без умовного WHERE (усе, крім 'hold') завжди повертають True — регресія на існуючі виклики."""
    conn = FakeConnection()
    assert await update_user_balance(user_id=1, amount_kwh=1.0, t_type="deposit", conn=conn) is True
    assert await update_user_balance(user_id=1, amount_kwh=1.0, t_type="ocpi_session", conn=conn) is True
    assert await update_user_balance(user_id=1, amount_kwh=1.0, t_type="refund", conn=conn) is True
