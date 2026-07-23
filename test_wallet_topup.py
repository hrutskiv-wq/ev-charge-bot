"""
Тести купівлі kWh-пакетів через Monobank-еквайринг (buy-side гаманця,
заміна тестового Telegram Payments) — app/api/wallet_webhook.py.

Та сама модель довіри, що й у станційному webhook
(test_operator_payments.py): тілу webhook не віримо, статус перепитуємо
в банку токеном оператора. Тут додатково перевіряється:

  * нарахування kWh-балансу йде через update_user_balance() (єдина точка)
    з payment_id, що вказує на рядок у ЗАГАЛЬНІЙ таблиці payments —
    kw_transactions.payment_id є FK саме на payments(id), а не на
    wallet_topups(id) чи operator_payments(id), тому напряму
    wallet_topups.id туди підставити не можна;
  * ідемпотентність подвійного webhook (мʼютекс set_wallet_topup_status +
    ON CONFLICT на payments.invoice_id — подвійний захист);
  * повна ізольованість від станційного шляху: wallet-webhook НІКОЛИ не
    звертається до operator_payments/operator_sessions.

Живих мережевих викликів і живої Postgres немає — той самий підхід
фейкових з'єднань, що в test_operator_payments.py/test_operator_isolation.py.

Запуск: pytest test_wallet_topup.py -v
"""
import json
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from app.api import wallet_webhook
from app.core import crypto
from app.database import operators_repo as repo

OPERATOR_A = 1
OPERATOR_B = 2
INVOICE = "inv-wallet-abc"


@pytest.fixture
def encryption_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(crypto.ENV_VAR, key)
    crypto.reset_cache()
    yield key
    crypto.reset_cache()


# ---------------------------------------------------------------------------
# Фейкова модель стану: поповнення, рядки payments, баланс, сповіщення
# ---------------------------------------------------------------------------

class FakeWalletState:
    def __init__(self):
        self.topups = {}       # (operator_id, invoice_id) -> dict
        self.tokens = {}       # operator_id -> encrypted token
        self.payments = {}     # invoice_id -> dict (рядок у payments)
        self.balance_calls = []
        self.status_calls = 0
        self.notifications = []
        # Якщо станційний код колись помилково викликається з wallet-шляху —
        # ці виклики впадуть голосно, а не мовчки повернуть щось правдоподібне.
        self.station_path_touched = False

    def add_topup(self, operator_id, invoice_id, user_id=555, package="pack_50",
                 kwh=50.0, amount_uah=None, status="pending", topup_id=None):
        topup_id = topup_id or (len(self.topups) + 1)
        self.topups[(operator_id, invoice_id)] = {
            "id": topup_id, "operator_id": operator_id, "user_id": user_id,
            "invoice_id": invoice_id, "package": package, "kwh": kwh,
            "amount_uah": amount_uah if amount_uah is not None else Decimal("750.00"),
            "status": status,
        }
        return topup_id


class FakeConn:
    """
    Заглушка з'єднання для credit_wallet_topup()/apply_wallet_topup_status():
    INSERT payments, + гачок _fail_next_insert, щоб один конкретний тест міг
    змусити ЦЕЙ INSERT кинути виняток посеред транзакції (регресія на
    блокер #1 — крах між "статус -> success" і фактичним нарахуванням).
    """

    def __init__(self, state):
        self.state = state
        self._fail_next_insert = False

    def is_in_transaction(self):
        """
        Продовий код (app/api/wallet_webhook.py::_credit_wallet_topup_in_conn)
        явно перевіряє це на вході — усі наявні виклики в тестах ідуть через
        transaction() нижче, тож тут завжди True.
        """
        return True

    async def fetchval(self, query, *args):
        assert "INSERT INTO payments" in query
        assert "ON CONFLICT (invoice_id) DO NOTHING" in query
        if self._fail_next_insert:
            self._fail_next_insert = False
            raise RuntimeError("симуляція збою БД посеред транзакції")
        user_id, invoice_id, amount, payload = args
        if invoice_id in self.state.payments:
            return None
        payment_id = len(self.state.payments) + 100
        self.state.payments[invoice_id] = {
            "id": payment_id, "user_id": user_id, "invoice_id": invoice_id,
            "amount": amount, "payload": payload,
        }
        return payment_id

    def transaction(self):
        return _FakeTxn(self.state)


class _FakeTxn:
    """
    Мінімальна, але ЧЕСНА симуляція ROLLBACK: знімає копію мутованого стану
    на вході, і якщо блок вийшов через виняток — повертає стан назад. Без
    цього фейкове з'єднання моделювало б лише "щасливий шлях" (COMMIT), і
    регресійний тест на блокер #1 (атомарність мʼютексу + нарахування) не
    міг би нічого довести — обидві мутації (статус поповнення й рядок
    payments) відбуваються через РІЗНІ фейки (repo.set_wallet_topup_status
    і FakeConn.fetchval відповідно), тому знімок береться по всьому стану,
    а не по одній таблиці.
    """

    def __init__(self, state):
        self.state = state
        self._topups_snapshot = None
        self._payments_snapshot = None
        self._balance_calls_len = None

    async def __aenter__(self):
        self._topups_snapshot = {k: dict(v) for k, v in self.state.topups.items()}
        self._payments_snapshot = {k: dict(v) for k, v in self.state.payments.items()}
        self._balance_calls_len = len(self.state.balance_calls)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.state.topups = self._topups_snapshot
            self.state.payments = self._payments_snapshot
            del self.state.balance_calls[self._balance_calls_len:]
        return False  # виняток НЕ глушимо — має пробитись, як і в реальній БД


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


@pytest.fixture
def wallet(monkeypatch):
    state = FakeWalletState()
    conn = FakeConn(state)
    state.conn = conn  # доступ з тестів, щоб зʼявити _fail_next_insert

    async def get_wallet_topup_by_invoice(operator_id, invoice_id):
        return state.topups.get((operator_id, invoice_id))

    async def get_operator_monobank_token_encrypted(operator_id):
        return state.tokens.get(operator_id)

    async def set_wallet_topup_status(operator_id, topup_id, status, payload=None, conn=None):
        for topup in state.topups.values():
            if topup["operator_id"] == operator_id and topup["id"] == topup_id:
                if topup["status"] == status:
                    return False
                topup["status"] = status
                return True
        return False

    async def get_operator_payment_by_invoice(operator_id, invoice_id):
        state.station_path_touched = True
        return None

    async def get_db_pool():
        return FakePool(conn)

    async def update_user_balance(user_id, amount_kwh, t_type="deposit", conn=None,
                                  session_id=None, payment_id=None, description=None):
        state.balance_calls.append({
            "user_id": user_id, "amount_kwh": amount_kwh, "t_type": t_type,
            "payment_id": payment_id, "description": description,
        })

    for name, func in [
        ("get_wallet_topup_by_invoice", get_wallet_topup_by_invoice),
        ("get_operator_monobank_token_encrypted", get_operator_monobank_token_encrypted),
        ("set_wallet_topup_status", set_wallet_topup_status),
        ("get_operator_payment_by_invoice", get_operator_payment_by_invoice),
    ]:
        monkeypatch.setattr(repo, name, func)

    monkeypatch.setattr(wallet_webhook.db_conn, "get_db_pool", get_db_pool)
    monkeypatch.setattr(wallet_webhook.db_conn, "update_user_balance", update_user_balance)

    async def fake_notify(user_id, kwh, amount_uah):
        state.notifications.append({"user_id": user_id, "kwh": kwh, "amount_uah": amount_uah})
        return True

    monkeypatch.setattr(wallet_webhook, "_notify_driver_credited", fake_notify)

    return state


@pytest.fixture
def bank(monkeypatch, wallet):
    """Підмінює виклик банку. bank.reply — те, що «скаже» банк."""
    class FakeBank:
        reply = {"status": "success", "amount": 75000}
        error = None

    async def get_invoice_status(token, invoice_id):
        wallet.status_calls += 1
        if FakeBank.error:
            raise FakeBank.error
        return dict(FakeBank.reply, invoiceId=invoice_id)

    monkeypatch.setattr(wallet_webhook, "get_invoice_status", get_invoice_status)
    return FakeBank


class FakeRequest:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode() if payload is not None else b""

    async def body(self):
        return self._body


async def _post(operator_id, payload):
    return await wallet_webhook.wallet_topup_webhook(operator_id, FakeRequest(payload))


@pytest.fixture
def paid_setup(wallet, encryption_key):
    """Оператор А з токеном і pending-поповненням pack_50 (750 грн) для user_id=555."""
    wallet.tokens[OPERATOR_A] = crypto.encrypt_secret("merchant-token-A")
    wallet.add_topup(OPERATOR_A, INVOICE, user_id=555, package="pack_50",
                     kwh=50.0, amount_uah=Decimal("750.00"), topup_id=5)
    return wallet


# --- нарахування балансу після оплати -----------------------------------------

async def test_webhook_credits_balance_when_bank_confirms(paid_setup, bank):
    bank.reply = {"status": "success", "amount": 75000}

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == "success"

    assert len(paid_setup.balance_calls) == 1
    call = paid_setup.balance_calls[0]
    assert call["user_id"] == 555
    assert call["amount_kwh"] == 50.0
    assert call["t_type"] == "deposit"
    assert call["payment_id"] == paid_setup.payments[INVOICE]["id"]
    assert "50.0" in call["description"] or "50" in call["description"]

    # Рядок у payments — щоб kw_transactions.payment_id мав на що посилатись.
    assert paid_setup.payments[INVOICE]["user_id"] == 555
    assert paid_setup.payments[INVOICE]["amount"] == Decimal("750.00")


async def test_pack_100_credits_100_kwh(wallet, bank, encryption_key):
    wallet.tokens[OPERATOR_A] = crypto.encrypt_secret("merchant-token-A")
    wallet.add_topup(OPERATOR_A, INVOICE, user_id=777, package="pack_100",
                     kwh=100.0, amount_uah=Decimal("1350.00"), topup_id=9)
    bank.reply = {"status": "success", "amount": 135000}

    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert len(wallet.balance_calls) == 1
    assert wallet.balance_calls[0]["amount_kwh"] == 100.0
    assert wallet.balance_calls[0]["user_id"] == 777


# --- тілу webhook не вірять ----------------------------------------------------

async def test_webhook_body_claiming_success_does_not_credit_if_bank_disagrees(
        paid_setup, bank):
    bank.reply = {"status": "created", "amount": 75000}

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE, "status": "success",
                                        "amount": 75000})

    assert response.status_code == 200
    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == "pending"
    assert paid_setup.balance_calls == []


async def test_amount_mismatch_blocks_credit(paid_setup, bank):
    bank.reply = {"status": "success", "amount": 100}  # 1 грн замість 750

    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == "pending"
    assert paid_setup.balance_calls == []


# --- ідемпотентність ------------------------------------------------------------

async def test_repeated_webhook_does_not_double_credit(paid_setup, bank):
    bank.reply = {"status": "success", "amount": 75000}

    await _post(OPERATOR_A, {"invoiceId": INVOICE})
    calls_after_first = paid_setup.status_calls
    await _post(OPERATOR_A, {"invoiceId": INVOICE})
    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert len(paid_setup.balance_calls) == 1, "Баланс нараховано більше одного разу"
    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == "success"
    assert paid_setup.status_calls == calls_after_first


async def test_crash_between_status_flip_and_credit_does_not_lose_the_topup(
        paid_setup, bank):
    """
    РЕГРЕСІЯ на блокер #1 з незалежного рев'ю: раніше "статус -> success" і
    фактичне нарахування (INSERT payments + update_user_balance) йшли ДВОМА
    окремими транзакціями — крах процесу між ними лишав поповнення
    'success' БЕЗ грошей, а повторний webhook бачив уже 'success' і тихо
    виходив (200, нічого не роблячи). Для wallet_topups немає звірки на
    відміну від operator_payments/reconcile_operators.py — втрата була б
    постійною й безшумною.

    Тепер мʼютекс і нарахування — в ОДНІЙ транзакції: симулюємо крах САМЕ
    посеред неї (INSERT payments кидає виняток) і перевіряємо, що ОБИДВА
    кроки відкотились разом, а не лишили половинчастий стан.
    """
    bank.reply = {"status": "success", "amount": 75000}
    paid_setup.conn._fail_next_insert = True

    with pytest.raises(RuntimeError):
        await _post(OPERATOR_A, {"invoiceId": INVOICE})

    # Відкат: статус НЕ застряг на 'success' без грошей — інакше повторний
    # webhook побачив би topup["status"] == "success" (рядок-перевірка на
    # початку wallet_topup_webhook) і тихо вийшов, а гроші так і не прийшли б.
    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == "pending", (
        "Статус лишився 'success' без грошей — саме той блокер, що фіксує цей тест"
    )
    assert paid_setup.balance_calls == [], "Гроші не мали нарахуватись при відкоченій транзакції"
    assert INVOICE not in paid_setup.payments

    # Повторний (справний) webhook добирає нарахування чисто.
    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == "success"
    assert len(paid_setup.balance_calls) == 1
    assert paid_setup.balance_calls[0]["amount_kwh"] == 50.0


async def test_second_credit_call_is_blocked_even_if_status_mutex_bypassed(
        paid_setup, bank, monkeypatch):
    """
    Друга лінія захисту: навіть якби мʼютекс set_wallet_topup_status чомусь
    не спрацював (гонка), ON CONFLICT (invoice_id) DO NOTHING на payments не
    дасть нарахувати вдруге — credit_wallet_topup() поверне 'already_processed'.
    """
    paid_setup.payments[INVOICE] = {"id": 999, "user_id": 555,
                                    "invoice_id": INVOICE, "amount": Decimal("750.00")}

    topup = paid_setup.topups[(OPERATOR_A, INVOICE)]
    outcome = await wallet_webhook.credit_wallet_topup(topup)

    assert outcome == "already_processed"
    assert paid_setup.balance_calls == []


# --- ізоляція тенантів і повна відокремленість від станційного шляху -----------

async def test_webhook_of_another_operator_cannot_confirm_foreign_invoice(
        paid_setup, bank):
    response = await _post(OPERATOR_B, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert paid_setup.status_calls == 0
    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == "pending"
    assert paid_setup.balance_calls == []


async def test_unknown_invoice_answers_quietly_without_details(paid_setup, bank):
    unknown = await _post(OPERATOR_A, {"invoiceId": "не-існує"})
    known = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert unknown.status_code == known.status_code == 200
    assert unknown.body == known.body == b""


async def test_wallet_webhook_never_touches_operator_payments_table(paid_setup, bank):
    """
    Розрізнення wallet_topup vs станційна сесія — структурне (окремий
    роут/окрема таблиця), а не рядок коду, що можна забути. Якщо
    wallet-webhook колись почне звертатись до operator_payments — тест
    впаде тут.
    """
    await _post(OPERATOR_A, {"invoiceId": INVOICE})
    assert paid_setup.station_path_touched is False


@pytest.mark.parametrize("payload", [
    {},
    {"invoiceId": ""},
    {"status": "success"},
    None,
])
async def test_malformed_webhook_bodies_are_ignored_quietly(payload, paid_setup, bank):
    response = await _post(OPERATOR_A, payload)
    assert response.status_code == 200
    assert paid_setup.status_calls == 0
    assert paid_setup.balance_calls == []


# --- інші відповіді банку -----------------------------------------------------

@pytest.mark.parametrize("bank_status,expected", [
    ("failure", "failed"),
    ("expired", "expired"),
    ("reversed", "reversed"),
])
async def test_final_negative_bank_statuses_are_recorded_without_credit(
        bank_status, expected, paid_setup, bank):
    bank.reply = {"status": bank_status, "amount": 75000}

    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == expected
    assert paid_setup.balance_calls == []


@pytest.mark.parametrize("bank_status", ["created", "processing", "hold"])
async def test_intermediate_bank_statuses_change_nothing(bank_status, paid_setup, bank):
    bank.reply = {"status": bank_status, "amount": 75000}

    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == "pending"
    assert paid_setup.balance_calls == []


async def test_bank_unavailable_returns_502_so_monobank_retries(paid_setup, bank):
    from app.services import monobank_acquiring
    bank.error = monobank_acquiring.MonobankError("connection refused")

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 502
    assert paid_setup.balance_calls == []


async def test_operator_without_stored_token_is_handled_quietly(wallet, bank,
                                                                 encryption_key):
    wallet.add_topup(OPERATOR_A, INVOICE, topup_id=5)

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert wallet.status_calls == 0
    assert wallet.balance_calls == []


# --- сповіщення водія -----------------------------------------------------------

async def test_driver_is_notified_after_successful_topup(paid_setup, bank):
    bank.reply = {"status": "success", "amount": 75000}

    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert len(paid_setup.notifications) == 1
    push = paid_setup.notifications[0]
    assert push["user_id"] == 555
    assert push["kwh"] == 50.0
    assert push["amount_uah"] == Decimal("750.00")


async def test_failed_notification_does_not_undo_the_credit(paid_setup, bank, monkeypatch):
    """
    Гроші вже нараховані. Якщо Telegram недоступний, webhook усе одно має
    відповісти 200 — інакше банк повторить уже оброблений платіж. Захист
    подвійний (credit_wallet_topup обгортає виклик try/except окремо від
    _notify_driver_credited) — навмисно підміняємо саме на функцію, що
    кидає, щоб перевірити зовнішній шар, а не покладатись лише на внутрішній.
    """
    async def broken_notify(user_id, kwh, amount_uah):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(wallet_webhook, "_notify_driver_credited", broken_notify)
    bank.reply = {"status": "success", "amount": 75000}

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200, (
        "Збій сповіщення дав не-2xx — банк ретраїтиме вже оброблений платіж"
    )
    assert paid_setup.topups[(OPERATOR_A, INVOICE)]["status"] == "success"
    assert len(paid_setup.balance_calls) == 1
