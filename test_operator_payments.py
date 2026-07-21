"""
Тести Промпту 2a: шифрування секретів операторів, клієнт Monobank
Acquiring і webhook підтвердження оплати.

Головне, що тут перевіряється — модель довіри webhook: тіло запиту НЕ є
джерелом правди. Навіть якщо надіслати ідеально сформований webhook, який
каже «оплачено», нарахування не станеться, поки того ж не скаже банк.

Живих мережевих викликів і живої Postgres немає: банк підмінюється
фейковою функцією, пул — фейковим з'єднанням (як у test_balance.py та
test_operator_isolation.py).

Запуск: pytest test_operator_payments.py -v
"""
import json
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from app.api import operator_webhook
from app.core import crypto
from app.database import operators_repo as repo
from app.services import monobank_acquiring

OPERATOR_A = 1
OPERATOR_B = 2
INVOICE = "inv-abc123"


# ---------------------------------------------------------------------------
# 1. Шифрування секретів (app/core/crypto.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def encryption_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(crypto.ENV_VAR, key)
    crypto.reset_cache()
    yield key
    crypto.reset_cache()


def test_secret_survives_encrypt_decrypt_roundtrip(encryption_key):
    token = "uXQ7z_test_merchant_token"
    encrypted = crypto.encrypt_secret(token)

    assert encrypted != token, "Токен збережено відкритим текстом"
    assert token not in encrypted
    assert crypto.decrypt_secret(encrypted) == token


def test_same_secret_encrypts_differently_each_time(encryption_key):
    """Fernet додає випадковий IV — два шифротексти не збігаються."""
    a = crypto.encrypt_secret("той самий токен")
    b = crypto.encrypt_secret("той самий токен")
    assert a != b
    assert crypto.decrypt_secret(a) == crypto.decrypt_secret(b)


def test_missing_key_fails_loudly_only_when_actually_used(monkeypatch):
    """
    Без ENCRYPTION_KEY застосунок має стартувати (решта функціоналу від
    ключа не залежить), але спроба зашифрувати — падати з поясненням.
    """
    monkeypatch.delenv(crypto.ENV_VAR, raising=False)
    crypto.reset_cache()

    assert crypto.is_configured() is False
    assert crypto.warn_if_key_missing() is False  # старт не падає

    with pytest.raises(crypto.EncryptionKeyMissing) as exc:
        crypto.encrypt_secret("secret")

    message = str(exc.value)
    assert "ENCRYPTION_KEY не задано" in message
    assert "білінг операторів непрацездатний" in message
    assert "Fernet.generate_key" in message  # підказка, як полагодити


def test_secret_encrypted_with_another_key_is_not_silently_accepted(monkeypatch):
    """
    Підміна ENCRYPTION_KEY не має «тихо» ламати білінг: розшифрування
    чужим ключем падає з поясненням, а не повертає сміття.
    """
    monkeypatch.setenv(crypto.ENV_VAR, Fernet.generate_key().decode())
    crypto.reset_cache()
    encrypted = crypto.encrypt_secret("merchant-token")

    monkeypatch.setenv(crypto.ENV_VAR, Fernet.generate_key().decode())
    crypto.reset_cache()
    with pytest.raises(crypto.EncryptionKeyMissing) as exc:
        crypto.decrypt_secret(encrypted)
    assert "перешифрування" in str(exc.value)


def test_invalid_key_value_is_rejected_with_actionable_message(monkeypatch):
    monkeypatch.setenv(crypto.ENV_VAR, "це-не-fernet-ключ")
    crypto.reset_cache()
    with pytest.raises(crypto.EncryptionKeyMissing) as exc:
        crypto.encrypt_secret("secret")
    assert "не валідний Fernet-ключ" in str(exc.value)


# ---------------------------------------------------------------------------
# 2. Гроші в копійках (app/services/monobank_acquiring.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount_uah,expected_kopecks", [
    (200, 20000),
    (Decimal("199.99"), 19999),
    # РЕГРЕСІЯ: int(19.99 * 100) == 1998 через двійкову похибку float —
    # водій платив би на копійку менше, ніж показала сторінка
    (19.99, 1999),
    (0.01, 1),
    (1234.56, 123456),
])
def test_uah_converted_to_kopecks_without_float_error(amount_uah, expected_kopecks):
    assert monobank_acquiring.uah_to_kopecks(amount_uah) == expected_kopecks


def test_kopecks_back_to_uah_is_decimal():
    value = monobank_acquiring.kopecks_to_uah(19999)
    assert isinstance(value, Decimal)
    assert value == Decimal("199.99")


# ---------------------------------------------------------------------------
# 3. Webhook: підміна репозиторію та банку
# ---------------------------------------------------------------------------

class FakeBilling:
    """
    Мінімальна модель стану білінгу в пам'яті: платежі, сесії, журнал.
    Відтворює ті інваріанти, які в проді тримає БД (унікальність інвойсу в
    межах оператора, ідемпотентність зміни статусу, один дохід на сесію).
    """

    def __init__(self):
        self.payments = {}   # (operator_id, invoice_id) -> dict
        self.sessions = {}   # (operator_id, payment_id) -> dict
        self.ledger = []
        self.tokens = {}     # operator_id -> encrypted token
        self.operators = {}  # operator_id -> dict
        self.status_calls = 0
        self.notifications = []

    def add_payment(self, operator_id, invoice_id, amount_uah, status="pending",
                    payment_id=None, session_id=None):
        payment_id = payment_id or (len(self.payments) + 1)
        self.payments[(operator_id, invoice_id)] = {
            "id": payment_id, "operator_id": operator_id, "invoice_id": invoice_id,
            "amount_uah": amount_uah, "status": status,
        }
        if session_id is not None:
            self.sessions[(operator_id, payment_id)] = {
                "id": session_id, "operator_id": operator_id, "status": "pending",
                "station_id": 10, "driver_contact": None,
            }
        return payment_id


@pytest.fixture
def billing(monkeypatch):
    state = FakeBilling()

    async def get_operator_payment_by_invoice(operator_id, invoice_id):
        return state.payments.get((operator_id, invoice_id))

    async def get_operator_monobank_token_encrypted(operator_id):
        return state.tokens.get(operator_id)

    async def set_operator_payment_status(operator_id, payment_id, status,
                                          payload=None, conn=None):
        for payment in state.payments.values():
            if payment["operator_id"] == operator_id and payment["id"] == payment_id:
                if payment["status"] == status:
                    return False  # той самий статус — рядок не змінився
                payment["status"] = status
                return True
        return False

    async def get_session_by_payment(operator_id, payment_id, conn=None):
        return state.sessions.get((operator_id, payment_id))

    async def get_operator(operator_id):
        return state.operators.get(
            operator_id,
            {"id": operator_id, "telegram_id": 500 + operator_id, "commission_pct": 4},
        )

    async def set_session_status(operator_id, session_id, status, payment_id=None):
        for session in state.sessions.values():
            if session["operator_id"] == operator_id and session["id"] == session_id:
                session["status"] = status
                return True
        return False

    async def record_session_income(operator_id, session_id, amount_uah, commission_pct):
        already = [e for e in state.ledger
                   if e["operator_id"] == operator_id and e["session_id"] == session_id]
        if already:
            return already[0]["id"], already[1]["id"]
        base = len(state.ledger) + 1
        state.ledger.append({"id": base, "operator_id": operator_id,
                             "session_id": session_id, "type": "session_income",
                             "amount_uah": amount_uah})
        state.ledger.append({"id": base + 1, "operator_id": operator_id,
                             "session_id": session_id, "type": "platform_commission",
                             "amount_uah": -amount_uah * commission_pct / 100})
        return base, base + 1

    async def get_station(operator_id, station_id):
        return {"id": station_id, "operator_id": operator_id, "name": "Готель Едем"}

    async def fake_notify(**kwargs):
        state.notifications.append(kwargs)
        return True

    monkeypatch.setattr(operator_webhook, "notify_operator_paid", fake_notify)

    for name, func in [
        ("get_station", get_station),
        ("get_operator_payment_by_invoice", get_operator_payment_by_invoice),
        ("get_operator_monobank_token_encrypted", get_operator_monobank_token_encrypted),
        ("set_operator_payment_status", set_operator_payment_status),
        ("get_session_by_payment", get_session_by_payment),
        ("get_operator", get_operator),
        ("set_session_status", set_session_status),
        ("record_session_income", record_session_income),
    ]:
        monkeypatch.setattr(repo, name, func)
    return state


@pytest.fixture
def bank(monkeypatch, billing):
    """Підмінює виклик банку. bank.reply — те, що «скаже» банк."""
    class FakeBank:
        reply = {"status": "success", "amount": 20000}
        error = None

    async def get_invoice_status(token, invoice_id):
        billing.status_calls += 1
        if FakeBank.error:
            raise FakeBank.error
        return dict(FakeBank.reply, invoiceId=invoice_id)

    monkeypatch.setattr(operator_webhook, "get_invoice_status", get_invoice_status)
    return FakeBank


class FakeRequest:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode() if payload is not None else b""

    async def body(self):
        return self._body


async def _post(operator_id, payload):
    return await operator_webhook.operator_invoice_webhook(operator_id, FakeRequest(payload))


@pytest.fixture
def paid_setup(billing, encryption_key):
    """Оператор А з токеном, сесією #77 і pending-інвойсом на 200 грн."""
    billing.tokens[OPERATOR_A] = crypto.encrypt_secret("merchant-token-A")
    billing.add_payment(OPERATOR_A, INVOICE, Decimal("200.00"),
                        payment_id=5, session_id=77)
    return billing


# --- головне: тілу webhook не вірять -----------------------------------------

async def test_webhook_body_claiming_success_does_not_credit_if_bank_disagrees(
        paid_setup, bank):
    """
    КЛЮЧОВИЙ ТЕСТ БЕЗПЕКИ. Зловмисник знає URL webhook і invoiceId, шле
    ідеальне «оплачено». Банк каже, що інвойс лише створений — нарахування
    не має статись.
    """
    bank.reply = {"status": "created", "amount": 20000}

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE, "status": "success",
                                        "amount": 20000})

    assert response.status_code == 200  # тихо, без подробиць
    assert paid_setup.payments[(OPERATOR_A, INVOICE)]["status"] == "pending"
    assert paid_setup.ledger == [], "Дохід нараховано з тіла webhook — це діра"
    assert paid_setup.sessions[(OPERATOR_A, 5)]["status"] == "pending"


async def test_webhook_credits_when_bank_confirms(paid_setup, bank):
    bank.reply = {"status": "success", "amount": 20000}

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert paid_setup.payments[(OPERATOR_A, INVOICE)]["status"] == "success"
    assert paid_setup.sessions[(OPERATOR_A, 5)]["status"] == "paid"
    assert len(paid_setup.ledger) == 2
    assert paid_setup.ledger[0]["type"] == "session_income"
    assert paid_setup.ledger[0]["amount_uah"] == Decimal("200.00")


# --- ізоляція тенантів на рівні webhook ---------------------------------------

async def test_webhook_of_another_operator_cannot_confirm_foreign_invoice(
        paid_setup, bank):
    """
    Інвойс оператора А, надісланий на URL оператора Б, не знаходиться:
    пошук іде по (operator_id з URL, invoiceId). Банк навіть не питається.
    """
    response = await _post(OPERATOR_B, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert paid_setup.status_calls == 0, "Банк не мали питати про чужий інвойс"
    assert paid_setup.payments[(OPERATOR_A, INVOICE)]["status"] == "pending"
    assert paid_setup.ledger == []


async def test_unknown_invoice_answers_quietly_without_details(paid_setup, bank):
    """
    Невідомий invoiceId -> 200 без тіла. Відповідь не має відрізнятись від
    успішного випадку, інакше ендпоінт стає оракулом для зондування.
    """
    unknown = await _post(OPERATOR_A, {"invoiceId": "не-існує"})
    known = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert unknown.status_code == known.status_code == 200
    assert unknown.body == known.body == b""


@pytest.mark.parametrize("payload", [
    {},                      # без invoiceId
    {"invoiceId": ""},       # порожній
    {"status": "success"},   # тільки статус
    None,                    # порожнє тіло
])
async def test_malformed_webhook_bodies_are_ignored_quietly(payload, paid_setup, bank):
    response = await _post(OPERATOR_A, payload)
    assert response.status_code == 200
    assert paid_setup.status_calls == 0
    assert paid_setup.ledger == []


# --- ідемпотентність ----------------------------------------------------------

async def test_repeated_webhook_does_not_double_credit(paid_setup, bank):
    bank.reply = {"status": "success", "amount": 20000}

    await _post(OPERATOR_A, {"invoiceId": INVOICE})
    calls_after_first = paid_setup.status_calls
    await _post(OPERATOR_A, {"invoiceId": INVOICE})
    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert len(paid_setup.ledger) == 2, "Дохід нараховано більше одного разу"
    assert paid_setup.payments[(OPERATOR_A, INVOICE)]["status"] == "success"
    # Уже проведений платіж не змушує ходити в банк знову
    assert paid_setup.status_calls == calls_after_first


# --- інші відповіді банку -----------------------------------------------------

@pytest.mark.parametrize("bank_status,expected", [
    ("failure", "failed"),
    ("expired", "expired"),
    ("reversed", "reversed"),
])
async def test_final_negative_bank_statuses_are_recorded_without_income(
        bank_status, expected, paid_setup, bank):
    bank.reply = {"status": bank_status, "amount": 20000}

    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert paid_setup.payments[(OPERATOR_A, INVOICE)]["status"] == expected
    assert paid_setup.ledger == []
    assert paid_setup.sessions[(OPERATOR_A, 5)]["status"] == "pending"


@pytest.mark.parametrize("bank_status", ["created", "processing", "hold"])
async def test_intermediate_bank_statuses_change_nothing(bank_status, paid_setup, bank):
    bank.reply = {"status": bank_status, "amount": 20000}

    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert paid_setup.payments[(OPERATOR_A, INVOICE)]["status"] == "pending"
    assert paid_setup.ledger == []


async def test_amount_mismatch_blocks_income(paid_setup, bank):
    """
    Банк каже «оплачено», але на іншу суму, ніж ми виставляли. Краще не
    нарахувати й покликати людину, ніж провести невідому суму.
    """
    bank.reply = {"status": "success", "amount": 100}  # 1 грн замість 200

    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert paid_setup.payments[(OPERATOR_A, INVOICE)]["status"] == "pending"
    assert paid_setup.ledger == []


async def test_bank_unavailable_returns_502_so_monobank_retries(paid_setup, bank):
    """
    Якщо банк недоступний, оплата не має загубитись: віддаємо не-2xx, щоб
    Monobank повторив webhook пізніше.
    """
    bank.error = monobank_acquiring.MonobankError("connection refused")

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 502
    assert paid_setup.ledger == []


async def test_operator_without_stored_token_is_handled_quietly(billing, bank,
                                                                encryption_key):
    """Токен еквайрингу не збережений — підтвердити оплату нічим."""
    billing.add_payment(OPERATOR_A, INVOICE, Decimal("200.00"),
                        payment_id=5, session_id=77)

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert billing.status_calls == 0
    assert billing.ledger == []


async def test_paid_invoice_without_session_does_not_record_income(billing, bank,
                                                                   encryption_key):
    """
    Платіж успішний, але сесії до нього немає — дохід проводити нікуди.
    Позначаємо платіж і кличемо людину, а не вигадуємо сесію.
    """
    billing.tokens[OPERATOR_A] = crypto.encrypt_secret("merchant-token-A")
    billing.add_payment(OPERATOR_A, INVOICE, Decimal("200.00"), payment_id=5)
    bank.reply = {"status": "success", "amount": 20000}

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200
    assert billing.payments[(OPERATOR_A, INVOICE)]["status"] == "success"
    assert billing.ledger == []


async def test_operator_is_notified_after_successful_payment(paid_setup, bank):
    """
    Пуш «Оплачено, увімкніть станцію» — останній крок після проведення
    оплати, з усім, що потрібно оператору, щоб діяти.
    """
    bank.reply = {"status": "success", "amount": 20000}

    await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert len(paid_setup.notifications) == 1
    push = paid_setup.notifications[0]
    assert push["telegram_id"] == 501          # telegram_id оператора A
    assert push["session_id"] == 77
    assert push["amount_uah"] == Decimal("200.00")
    assert push["station_name"] == "Готель Едем"


async def test_failed_notification_does_not_undo_the_payment(paid_setup, bank,
                                                             monkeypatch):
    """
    Гроші вже прийшли й дохід проведено. Якщо Telegram недоступний, webhook
    усе одно має відповісти 200 — інакше банк повторить уже оброблений платіж.
    """
    async def broken_notify(**kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(operator_webhook, "notify_operator_paid", broken_notify)
    bank.reply = {"status": "success", "amount": 20000}

    response = await _post(OPERATOR_A, {"invoiceId": INVOICE})

    assert response.status_code == 200, (
        "Збій сповіщення дав не-2xx — банк ретраїтиме вже оброблений платіж"
    )
    assert paid_setup.payments[(OPERATOR_A, INVOICE)]["status"] == "success"
    assert len(paid_setup.ledger) == 2
