"""
Тести на reconcile_operators.py (Промпт 5).

Той самий стиль, що й test_operator_payments.py: живої Postgres і мережі
немає, банк і репозиторій підмінені фейковими функціями в памʼяті. Тут
додатково перевіряється те, чого немає у Промпті 2a/2b: цикл по кількох
операторах, ізоляція тенантів усередині звірки, ідемпотентність повторного
прогону і те, що недоступний банк одного оператора не зупиняє звірку решти.

Запуск: pytest test_reconcile_operators.py -v
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

import reconcile_operators
from app.api import operator_webhook
from app.core import crypto
from app.database import operators_repo as repo
from app.services import monobank_acquiring

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
OLD_ENOUGH = NOW - timedelta(seconds=monobank_acquiring.INVOICE_TTL_SECONDS + 60)
NOT_OLD_ENOUGH = NOW - timedelta(seconds=60)
STALE_SESSION = NOW - timedelta(minutes=reconcile_operators.STALE_SESSION_MINUTES + 5)
FRESH_SESSION = NOW - timedelta(minutes=5)

OPERATOR_A = 1
OPERATOR_B = 2


class FakeReconcileBilling:
    """Мінімальна модель стану операторського білінгу в пам'яті для звірки."""

    def __init__(self):
        self.operators = {}
        self.tokens = {}
        self.payments = {}
        self.sessions = {}
        self.ledger = []
        self.stations = {}
        self.notifications = []
        self.bank_down_tokens = set()
        self.bank_replies = {}
        self._next_payment_id = 1
        self._next_session_id = 1

    def add_operator(self, operator_id, name="Оператор", telegram_id=None,
                     commission_pct=4, status="active"):
        self.operators[operator_id] = {
            "id": operator_id, "name": name, "phone": None,
            "telegram_id": telegram_id if telegram_id is not None else 500 + operator_id,
            "status": status, "commission_pct": commission_pct,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }

    def add_payment(self, operator_id, invoice_id, amount_uah, status, created_at,
                    payment_id=None):
        payment_id = payment_id or self._next_payment_id
        self._next_payment_id = max(self._next_payment_id, payment_id + 1)
        self.payments[payment_id] = {
            "id": payment_id, "operator_id": operator_id, "invoice_id": invoice_id,
            "amount_uah": amount_uah, "status": status,
            "created_at": created_at, "updated_at": created_at,
        }
        return payment_id

    def add_session(self, operator_id, station_id, status="pending", payment_id=None,
                    amount_uah=None, created_at=None, session_id=None):
        session_id = session_id or self._next_session_id
        self._next_session_id = max(self._next_session_id, session_id + 1)
        self.sessions[session_id] = {
            "id": session_id, "operator_id": operator_id, "station_id": station_id,
            "status": status, "payment_id": payment_id, "amount_uah": amount_uah,
            "created_at": created_at or NOW, "driver_contact": None,
        }
        return session_id

    def add_station(self, operator_id, station_id, name):
        self.stations[(operator_id, station_id)] = {"id": station_id, "name": name}


@pytest.fixture
def encryption_key(monkeypatch):
    """
    reconcile_operators.py розшифровує токени оператора через справжній
    app.core.crypto (не мокається) — щоб звірка перевіряла ту саму
    поведінку, що й production. Без ключа decrypt_secret() падає ще до
    звернення до банку, тож будь-який тест зі сценарієм 1 залежить від
    цієї фікстури.
    """
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(crypto.ENV_VAR, key)
    crypto.reset_cache()
    yield key
    crypto.reset_cache()


@pytest.fixture
def billing(monkeypatch):
    state = FakeReconcileBilling()

    async def list_operators():
        return list(state.operators.values())

    async def get_operator(operator_id):
        return state.operators.get(operator_id)

    async def get_operator_monobank_token_encrypted(operator_id):
        return state.tokens.get(operator_id)

    async def list_pending_payments_older_than(operator_id, older_than):
        return [dict(p) for p in state.payments.values()
                if p["operator_id"] == operator_id and p["status"] == "pending"
                and p["created_at"] < older_than]

    async def set_operator_payment_status(operator_id, payment_id, status,
                                          payload=None, conn=None):
        p = state.payments.get(payment_id)
        if p is None or p["operator_id"] != operator_id or p["status"] == status:
            return False
        p["status"] = status
        return True

    async def get_session_by_payment(operator_id, payment_id, conn=None):
        for s in state.sessions.values():
            if s["operator_id"] == operator_id and s.get("payment_id") == payment_id:
                return dict(s)
        return None

    async def set_session_status(operator_id, session_id, status, payment_id=None):
        s = state.sessions.get(session_id)
        if s is None or s["operator_id"] != operator_id:
            return False
        s["status"] = status
        if payment_id is not None:
            s["payment_id"] = payment_id
        return True

    async def record_session_income(operator_id, session_id, amount_uah, commission_pct):
        existing = {e["type"]: e["id"] for e in state.ledger
                    if e["operator_id"] == operator_id and e["session_id"] == session_id
                    and e["type"] in ("session_income", "platform_commission")}
        if existing:
            return existing.get("session_income"), existing.get("platform_commission")
        base = len(state.ledger) + 1
        commission = Decimal(str(amount_uah)) * Decimal(str(commission_pct)) / 100
        state.ledger.append({"id": base, "operator_id": operator_id, "session_id": session_id,
                             "type": "session_income", "amount_uah": Decimal(str(amount_uah))})
        state.ledger.append({"id": base + 1, "operator_id": operator_id, "session_id": session_id,
                             "type": "platform_commission", "amount_uah": -commission})
        return base, base + 1

    async def get_station(operator_id, station_id):
        return state.stations.get((operator_id, station_id))

    async def list_success_payments_without_income(operator_id):
        rows = []
        for p in state.payments.values():
            if p["operator_id"] != operator_id or p["status"] != "success":
                continue
            session = next((s for s in state.sessions.values()
                            if s["operator_id"] == operator_id and s.get("payment_id") == p["id"]),
                           None)
            if session is None:
                continue
            has_income = any(e["operator_id"] == operator_id and e["session_id"] == session["id"]
                             and e["type"] == "session_income" for e in state.ledger)
            if not has_income:
                rows.append({**p, "session_id": session["id"]})
        return rows

    async def list_success_payments_without_session(operator_id):
        rows = []
        for p in state.payments.values():
            if p["operator_id"] != operator_id or p["status"] != "success":
                continue
            has_session = any(s["operator_id"] == operator_id and s.get("payment_id") == p["id"]
                              for s in state.sessions.values())
            if not has_session:
                rows.append(dict(p))
        return rows

    async def list_stale_pending_sessions_without_payment(operator_id, older_than):
        rows = []
        for s in state.sessions.values():
            if (s["operator_id"] != operator_id or s["status"] != "pending"
                    or s.get("payment_id") is not None or s["created_at"] >= older_than):
                continue
            station = state.stations.get((operator_id, s["station_id"]), {})
            rows.append({**s, "station_name": station.get("name", "станція")})
        return rows

    async def fake_notify(**kwargs):
        state.notifications.append(kwargs)
        return True

    for name, func in [
        ("list_operators", list_operators),
        ("get_operator", get_operator),
        ("get_operator_monobank_token_encrypted", get_operator_monobank_token_encrypted),
        ("list_pending_payments_older_than", list_pending_payments_older_than),
        ("set_operator_payment_status", set_operator_payment_status),
        ("get_session_by_payment", get_session_by_payment),
        ("set_session_status", set_session_status),
        ("record_session_income", record_session_income),
        ("get_station", get_station),
        ("list_success_payments_without_income", list_success_payments_without_income),
        ("list_success_payments_without_session", list_success_payments_without_session),
        ("list_stale_pending_sessions_without_payment", list_stale_pending_sessions_without_payment),
    ]:
        monkeypatch.setattr(repo, name, func)

    monkeypatch.setattr(operator_webhook, "notify_operator_paid", fake_notify)

    async def _noop():
        return None

    monkeypatch.setattr(reconcile_operators, "init_postgres", _noop)
    monkeypatch.setattr(reconcile_operators, "close_postgres", _noop)
    monkeypatch.delenv("LOGS_CHAT_ID", raising=False)

    return state


@pytest.fixture
def bank(monkeypatch, billing):
    async def get_invoice_status(token, invoice_id):
        if token in billing.bank_down_tokens:
            raise monobank_acquiring.MonobankError("connection refused")
        return billing.bank_replies.get(invoice_id, {"status": "created", "amount": None})

    monkeypatch.setattr(reconcile_operators, "get_invoice_status", get_invoice_status)
    return billing


async def _run():
    return await reconcile_operators.run(reconcile_operators.STALE_SESSION_MINUTES, now=NOW)


# ---------------------------------------------------------------------------
# 1. Сценарій 1: pending-платежі старші за INVOICE_TTL
# ---------------------------------------------------------------------------

async def test_stale_pending_payment_confirmed_by_bank_gets_credited(billing, bank, encryption_key):
    billing.add_operator(OPERATOR_A)
    billing.tokens[OPERATOR_A] = crypto.encrypt_secret("token-A")
    pid = billing.add_payment(OPERATOR_A, "inv-1", Decimal("200.00"), "pending", OLD_ENOUGH)
    billing.add_session(OPERATOR_A, 10, status="pending", payment_id=pid,
                        amount_uah=Decimal("200.00"))
    billing.bank_replies["inv-1"] = {"status": "success", "amount": 20000}

    exit_code = await _run()

    assert billing.payments[pid]["status"] == "success"
    assert len(billing.ledger) == 2
    assert billing.ledger[0]["type"] == "session_income"
    assert len(billing.notifications) == 1
    assert exit_code == 0


async def test_stale_pending_payment_expired_at_bank_is_recorded(billing, bank, encryption_key):
    billing.add_operator(OPERATOR_A)
    billing.tokens[OPERATOR_A] = crypto.encrypt_secret("token-A")
    pid = billing.add_payment(OPERATOR_A, "inv-2", Decimal("150.00"), "pending", OLD_ENOUGH)
    billing.bank_replies["inv-2"] = {"status": "expired", "amount": 15000}

    exit_code = await _run()

    assert billing.payments[pid]["status"] == "expired"
    assert billing.ledger == []
    assert exit_code == 0, "Проведений (нехай і негативний) статус — не привід для ручного розбору"


async def test_pending_payment_not_yet_old_enough_is_left_alone(billing, bank, encryption_key):
    """Інвойс молодший за TTL — ще законно 'processing', банк не питається."""
    billing.add_operator(OPERATOR_A)
    billing.tokens[OPERATOR_A] = crypto.encrypt_secret("token-A")
    pid = billing.add_payment(OPERATOR_A, "inv-3", Decimal("100.00"), "pending", NOT_OLD_ENOUGH)

    await _run()

    assert billing.payments[pid]["status"] == "pending"


async def test_pending_payment_still_processing_after_ttl_needs_manual_review(billing, bank, encryption_key):
    """Банк і після TTL каже 'processing' — підозріло, кличемо людину."""
    billing.add_operator(OPERATOR_A)
    billing.tokens[OPERATOR_A] = crypto.encrypt_secret("token-A")
    billing.add_payment(OPERATOR_A, "inv-4", Decimal("100.00"), "pending", OLD_ENOUGH)
    billing.bank_replies["inv-4"] = {"status": "processing", "amount": 10000}

    exit_code = await _run()

    assert exit_code == 1


# ---------------------------------------------------------------------------
# 2. Сценарій 2: success-платежі без доходу в журналі
# ---------------------------------------------------------------------------

async def test_success_payment_without_income_gets_backfilled(billing, bank):
    billing.add_operator(OPERATOR_A, commission_pct=5)
    pid = billing.add_payment(OPERATOR_A, "inv-5", Decimal("300.00"), "success", OLD_ENOUGH)
    billing.add_session(OPERATOR_A, 20, status="charging", payment_id=pid,
                        amount_uah=Decimal("300.00"))

    exit_code = await _run()

    assert len(billing.ledger) == 2
    assert billing.ledger[0]["amount_uah"] == Decimal("300.00")
    assert billing.sessions[pid]["status"] == "paid"
    assert len(billing.notifications) == 1
    assert exit_code == 0


async def test_success_payment_with_existing_income_is_not_touched_again(billing, bank):
    """Дохід уже проведений раніше (звичайний webhook) — звірка його не бачить і не чіпає."""
    billing.add_operator(OPERATOR_A)
    pid = billing.add_payment(OPERATOR_A, "inv-6", Decimal("300.00"), "success", OLD_ENOUGH)
    sid = billing.add_session(OPERATOR_A, 20, status="paid", payment_id=pid,
                              amount_uah=Decimal("300.00"))
    billing.ledger.append({"id": 1, "operator_id": OPERATOR_A, "session_id": sid,
                           "type": "session_income", "amount_uah": Decimal("300.00")})

    exit_code = await _run()

    assert len(billing.ledger) == 1
    assert billing.notifications == []
    assert exit_code == 0


# ---------------------------------------------------------------------------
# 3. Сценарій 3: success-платежі без сесії
# ---------------------------------------------------------------------------

async def test_success_payment_without_session_is_flagged_not_credited(billing, bank):
    billing.add_operator(OPERATOR_A)
    billing.add_payment(OPERATOR_A, "inv-7", Decimal("500.00"), "success", OLD_ENOUGH)

    exit_code = await _run()

    assert billing.ledger == []
    assert exit_code == 1


# ---------------------------------------------------------------------------
# 4. Сценарій 4: pending-сесії без payment_id, старші за годину
# ---------------------------------------------------------------------------

async def test_stale_pending_session_without_payment_is_flagged(billing, bank):
    billing.add_operator(OPERATOR_A)
    billing.add_station(OPERATOR_A, 30, "АЗС на Хрещатику")
    billing.add_session(OPERATOR_A, 30, status="pending", payment_id=None,
                        amount_uah=Decimal("250.00"), created_at=STALE_SESSION)

    exit_code = await _run()

    assert exit_code == 1


async def test_fresh_pending_session_without_payment_is_left_alone(billing, bank):
    """Водій щойно відкрив сторінку і ще не натиснув оплату — це не інцидент."""
    billing.add_operator(OPERATOR_A)
    billing.add_station(OPERATOR_A, 30, "АЗС на Хрещатику")
    billing.add_session(OPERATOR_A, 30, status="pending", payment_id=None,
                        amount_uah=Decimal("250.00"), created_at=FRESH_SESSION)

    exit_code = await _run()

    assert exit_code == 0


# ---------------------------------------------------------------------------
# 5. Ідемпотентність
# ---------------------------------------------------------------------------

async def test_second_run_does_not_double_credit_anything(billing, bank, encryption_key):
    billing.add_operator(OPERATOR_A)
    billing.tokens[OPERATOR_A] = crypto.encrypt_secret("token-A")
    pid = billing.add_payment(OPERATOR_A, "inv-8", Decimal("200.00"), "pending", OLD_ENOUGH)
    billing.add_session(OPERATOR_A, 10, status="pending", payment_id=pid,
                        amount_uah=Decimal("200.00"))
    billing.bank_replies["inv-8"] = {"status": "success", "amount": 20000}

    await _run()
    ledger_after_first = list(billing.ledger)
    notifications_after_first = len(billing.notifications)

    exit_code_second = await _run()

    assert billing.ledger == ledger_after_first, "Другий прогін задвоїв журнал"
    assert len(billing.notifications) == notifications_after_first, "Оператора сповістили вдруге"
    assert exit_code_second == 0


# ---------------------------------------------------------------------------
# 6. Ізоляція операторів
# ---------------------------------------------------------------------------

async def test_operator_a_problem_does_not_touch_operator_b(billing, bank, encryption_key):
    billing.add_operator(OPERATOR_A, name="Оператор А")
    billing.add_operator(OPERATOR_B, name="Оператор Б")
    billing.tokens[OPERATOR_B] = crypto.encrypt_secret("token-B")

    # Оператор А: платіж без сесії (проблема, потребує рук)
    billing.add_payment(OPERATOR_A, "inv-a", Decimal("400.00"), "success", OLD_ENOUGH)

    # Оператор Б: усе гаразд, а ще є платіж, що звірка мала б виправити
    pid_b = billing.add_payment(OPERATOR_B, "inv-b", Decimal("120.00"), "pending", OLD_ENOUGH)
    billing.add_session(OPERATOR_B, 99, status="pending", payment_id=pid_b,
                        amount_uah=Decimal("120.00"))
    billing.bank_replies["inv-b"] = {"status": "success", "amount": 12000}

    await _run()

    assert billing.payments[pid_b]["status"] == "success"
    assert any(e["operator_id"] == OPERATOR_B for e in billing.ledger)
    assert all(e["operator_id"] != OPERATOR_A for e in billing.ledger), (
        "Проблема оператора А не має проявлятись у журналі оператора Б"
    )


# ---------------------------------------------------------------------------
# 7. Недоступний банк для одного оператора не зриває весь прогін
# ---------------------------------------------------------------------------

async def test_bank_down_for_one_operator_does_not_block_others(billing, bank, encryption_key):
    billing.add_operator(OPERATOR_A, name="Оператор А")
    billing.add_operator(OPERATOR_B, name="Оператор Б")
    billing.tokens[OPERATOR_A] = crypto.encrypt_secret("token-A-down")
    billing.tokens[OPERATOR_B] = crypto.encrypt_secret("token-B")
    billing.bank_down_tokens.add("token-A-down")

    billing.add_payment(OPERATOR_A, "inv-down", Decimal("100.00"), "pending", OLD_ENOUGH)

    pid_b = billing.add_payment(OPERATOR_B, "inv-ok", Decimal("300.00"), "pending", OLD_ENOUGH)
    billing.add_session(OPERATOR_B, 55, status="pending", payment_id=pid_b,
                        amount_uah=Decimal("300.00"))
    billing.bank_replies["inv-ok"] = {"status": "success", "amount": 30000}

    exit_code = await _run()

    assert billing.payments[pid_b]["status"] == "success", (
        "Недоступний банк оператора А не мав завадити звірити оператора Б"
    )
    assert len(billing.ledger) == 2
    assert exit_code == 1, "Недоступний банк для когось з операторів — привід для ручної уваги"


async def test_operator_without_any_token_is_skipped_with_alert_not_crash(billing, bank):
    billing.add_operator(OPERATOR_A)
    billing.add_payment(OPERATOR_A, "inv-no-token", Decimal("100.00"), "pending", OLD_ENOUGH)

    exit_code = await _run()

    assert billing.payments[list(billing.payments.keys())[0]]["status"] == "pending"
    assert exit_code == 1


# ---------------------------------------------------------------------------
# 8. Підсумок пуша в Telegram
# ---------------------------------------------------------------------------

async def test_summary_pushed_to_telegram_when_logs_chat_id_set(billing, bank, monkeypatch):
    monkeypatch.setenv("LOGS_CHAT_ID", "-100999")

    sent = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(reconcile_operators, "_get_bot", lambda: FakeBot())

    billing.add_operator(OPERATOR_A)
    billing.add_payment(OPERATOR_A, "inv-push", Decimal("100.00"), "success", OLD_ENOUGH)

    await _run()

    assert len(sent) == 1
    assert sent[0]["chat_id"] == "-100999"
    assert "Звірка операторського білінгу" in sent[0]["text"]


async def test_summary_not_pushed_when_logs_chat_id_missing(billing, bank, monkeypatch):
    monkeypatch.delenv("LOGS_CHAT_ID", raising=False)
    calls = []
    monkeypatch.setattr(reconcile_operators, "_get_bot", lambda: calls.append(1))

    billing.add_operator(OPERATOR_A)
    billing.add_payment(OPERATOR_A, "inv-no-push", Decimal("100.00"), "success", OLD_ENOUGH)

    await _run()

    assert calls == []


async def test_telegram_push_failure_does_not_crash_reconciliation(billing, bank, monkeypatch):
    """Збій пуша (бот недоступний, мережа тощо) не має ламати саму звірку."""
    monkeypatch.setenv("LOGS_CHAT_ID", "-100999")

    def broken_get_bot():
        raise RuntimeError("bot недоступний")

    monkeypatch.setattr(reconcile_operators, "_get_bot", broken_get_bot)

    errors = []
    monkeypatch.setattr(reconcile_operators.logger, "error",
                        lambda *a, **k: errors.append(a))

    billing.add_operator(OPERATOR_A)
    billing.add_payment(OPERATOR_A, "inv-push-fail", Decimal("100.00"), "success", OLD_ENOUGH)

    exit_code = await _run()

    assert exit_code == 1  # платіж без сесії — це окрема, легітимна причина
    assert errors, "Збій пуша мав бути залогований, а не проковтнутий тихо без сліду"
