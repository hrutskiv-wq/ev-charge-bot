"""
Тести на reconcile_charging_reservations.py — модель A (Промпт 3c-i, kWh) і
модель B (Промпт 3c-ii, грн через Monobank hold/finalize/cancel).

kWh-частина (не змінена цим бандлом): той самий стиль, що
test_reconcile_operators.py — живої Postgres немає, репозиторій підмінений
фейком у пам'яті. Перевіряється: (1) пороги pending/active правильно
відсікають ще не застряглі резервації, (2) звільнення йде через
release_reservation_hold() (не напряму update_user_balance — та
атомарність уже перевірена в test_charging_reservations.py), (3) race з
паралельним StopTransaction (release_reservation_hold повертає False) не
рахується проблемою, (4) ідемпотентність — другий прогін нічого вже не
звільняє.

uah-частина (docs/plan-3c-ii.md, розділ 5): банк ЗАВЖДИ перепитується
(get_invoice_status) перед будь-якою дією — фейк симулює конкретну
відповідь банку per-invoice; finalize_invoice/cancel_invoice теж фейкові
(жодного реального HTTP). Перевіряється: (1) крок 1 (awaiting_hold) НІКОЛИ
не намагається сам стартувати зарядку — лише алерт, коли банк каже 'hold';
(2) крок 2 (pending) скасовує hold у банку, коли банк ще 'hold', і НЕ
дзвонить у банк повторно, коли той уже сам дійшов до фіналу; (3) крок 3
(active-uah, БЛОКЕР рев'ю — закрито) ловить застряглі 'active' резервації,
яких жоден інший запит не бачив (StopTransaction ніколи не прийшов, або
крах між completion сесії й claim); (4) крок 4 (settling) рахує вартість
тією самою чистою функцією, що й on_stop_transaction, і капує її на
утримане.

Запуск: pytest test_reconcile_charging_reservations.py -v
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import reconcile_charging_reservations as reconcile
from app.database import operators_repo as repo
from app.services.monobank_acquiring import MonobankError

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
OPERATOR_A = 1


class FakeReservationBilling:
    def __init__(self):
        self.reservations = {}
        self.sessions = {}
        self.stations = {}
        self.release_calls = []   # kwh: [(reservation_id, new_status), ...]
        self.cancel_calls = []    # uah: [invoice_id, ...]
        self.finalize_calls = []  # uah: [(invoice_id, amount_uah), ...]
        self.bank_status = {}     # invoice_id -> {"status": ..., "finalAmount": ...}
        self.operator_tokens = {}  # operator_id -> "encrypted-token"
        self.claim_calls = []     # [(reservation_id, expected_status), ...]
        self.claim_losses = set()  # reservation_id -> форсований програш заявки

    def add_reservation(self, reservation_id, operator_id, user_id, reserved_kwh,
                        status, created_at, updated_at=None):
        self.reservations[reservation_id] = {
            "id": reservation_id, "operator_id": operator_id, "station_id": 10,
            "user_id": user_id, "payment_method": "kwh", "reserved_kwh": reserved_kwh,
            "reserved_uah": None, "invoice_id": None, "final_amount_uah": None,
            "id_tag": f"tag-{reservation_id}", "status": status,
            "operator_session_id": 555 if status == "active" else None,
            "created_at": created_at, "updated_at": updated_at or created_at,
        }

    def add_uah_reservation(self, reservation_id, operator_id, invoice_id, reserved_uah,
                            status, created_at, updated_at=None, operator_session_id=None,
                            station_id=10):
        self.reservations[reservation_id] = {
            "id": reservation_id, "operator_id": operator_id, "station_id": station_id,
            "user_id": None, "payment_method": "uah", "reserved_kwh": None,
            "reserved_uah": reserved_uah, "invoice_id": invoice_id, "final_amount_uah": None,
            "id_tag": f"tag-{reservation_id}", "status": status,
            "operator_session_id": operator_session_id,
            "created_at": created_at, "updated_at": updated_at or created_at,
        }

    def set_bank_status(self, invoice_id, status, final_amount_kopecks=None):
        self.bank_status[invoice_id] = {"status": status, "finalAmount": final_amount_kopecks}

    def set_operator_token(self, operator_id, token="encrypted-token"):
        self.operator_tokens[operator_id] = token

    def add_session(self, session_id, operator_id, kwh, status="completed"):
        self.sessions[session_id] = {"id": session_id, "operator_id": operator_id, "kwh": kwh, "status": status}

    def add_station(self, station_id, operator_id, tariff_uah_kwh, tariff_uah_start=None):
        self.stations[station_id] = {
            "id": station_id, "operator_id": operator_id,
            "tariff_uah_kwh": tariff_uah_kwh, "tariff_uah_start": tariff_uah_start,
        }

    def force_claim_loss(self, reservation_id):
        self.claim_losses.add(reservation_id)


@pytest.fixture
def billing(monkeypatch):
    state = FakeReservationBilling()

    # --- kWh (модель A, без змін) ---

    async def list_stale_pending_reservations(older_than):
        return [dict(r) for r in state.reservations.values()
                if r["payment_method"] == "kwh" and r["status"] == "pending" and r["created_at"] < older_than]

    async def list_stale_active_reservations(older_than):
        return [dict(r) for r in state.reservations.values()
                if r["payment_method"] == "kwh" and r["status"] == "active" and r["updated_at"] < older_than]

    async def release_reservation_hold(operator_id, reservation_id, new_status):
        r = state.reservations.get(reservation_id)
        if r is None or r["operator_id"] != operator_id or r["payment_method"] != "kwh" \
                or r["status"] not in ("pending", "active"):
            return False
        r["status"] = new_status
        state.release_calls.append((reservation_id, new_status))
        return True

    # --- uah (модель B, Промпт 3c-ii) ---

    async def list_stale_awaiting_hold_reservations(older_than):
        return [dict(r) for r in state.reservations.values()
                if r["status"] == "awaiting_hold" and r["created_at"] < older_than]

    async def list_stale_pending_uah_reservations(older_than):
        return [dict(r) for r in state.reservations.values()
                if r["payment_method"] == "uah" and r["status"] == "pending" and r["created_at"] < older_than]

    async def list_stale_active_uah_reservations(older_than):
        return [dict(r) for r in state.reservations.values()
                if r["payment_method"] == "uah" and r["status"] == "active" and r["updated_at"] < older_than]

    async def list_stale_settling_reservations(older_than):
        return [dict(r) for r in state.reservations.values()
                if r["status"] == "settling" and r["updated_at"] < older_than]

    async def set_reservation_status(operator_id, reservation_id, status, conn=None):
        r = state.reservations.get(reservation_id)
        if r is None or r["operator_id"] != operator_id or r["status"] == status:
            return False
        r["status"] = status
        return True

    async def record_uah_settlement(operator_id, reservation_id, status, final_amount_uah):
        r = state.reservations.get(reservation_id)
        if r is None or r["operator_id"] != operator_id or r["status"] == status:
            return False
        r["status"] = status
        r["final_amount_uah"] = final_amount_uah
        return True

    async def claim_reservation_for_settlement(operator_id, reservation_id, expected_status="active"):
        state.claim_calls.append((reservation_id, expected_status))
        if reservation_id in state.claim_losses:
            return None
        r = state.reservations.get(reservation_id)
        if r is None or r["operator_id"] != operator_id or r["payment_method"] != "uah" \
                or r["status"] != expected_status:
            return None
        r["status"] = "settling"
        return {
            "reserved_uah": r["reserved_uah"], "invoice_id": r["invoice_id"],
            "operator_session_id": r["operator_session_id"],
        }

    async def get_operator_monobank_token_encrypted(operator_id):
        return state.operator_tokens.get(operator_id)

    async def get_session(operator_id, session_id):
        s = state.sessions.get(session_id)
        if s is None or s["operator_id"] != operator_id:
            return None
        return s

    async def get_station(operator_id, station_id):
        s = state.stations.get(station_id)
        if s is None or s["operator_id"] != operator_id:
            return None
        return s

    for name, func in [
        ("list_stale_pending_reservations", list_stale_pending_reservations),
        ("list_stale_active_reservations", list_stale_active_reservations),
        ("release_reservation_hold", release_reservation_hold),
        ("list_stale_awaiting_hold_reservations", list_stale_awaiting_hold_reservations),
        ("list_stale_pending_uah_reservations", list_stale_pending_uah_reservations),
        ("list_stale_active_uah_reservations", list_stale_active_uah_reservations),
        ("list_stale_settling_reservations", list_stale_settling_reservations),
        ("set_reservation_status", set_reservation_status),
        ("record_uah_settlement", record_uah_settlement),
        ("claim_reservation_for_settlement", claim_reservation_for_settlement),
        ("get_operator_monobank_token_encrypted", get_operator_monobank_token_encrypted),
        ("get_session", get_session),
        ("get_station", get_station),
    ]:
        monkeypatch.setattr(repo, name, func)

    # decrypt_secret — токен у фейку вже "звичайний рядок", розшифрування
    # тотожне (не тестуємо тут крипто-шар окремо).
    monkeypatch.setattr(reconcile, "decrypt_secret", lambda enc: enc)

    async def get_invoice_status(operator_token, invoice_id):
        return dict(state.bank_status.get(invoice_id, {"status": "hold"}))

    async def cancel_invoice(operator_token, invoice_id):
        state.cancel_calls.append(invoice_id)
        return {"status": "success"}

    async def finalize_invoice(operator_token, invoice_id, amount_uah):
        state.finalize_calls.append((invoice_id, amount_uah))
        return {"status": "success"}

    monkeypatch.setattr(reconcile, "get_invoice_status", get_invoice_status)
    monkeypatch.setattr(reconcile, "cancel_invoice", cancel_invoice)
    monkeypatch.setattr(reconcile, "finalize_invoice", finalize_invoice)

    async def _noop():
        return None

    monkeypatch.setattr(reconcile, "init_postgres", _noop)
    monkeypatch.setattr(reconcile, "close_postgres", _noop)
    monkeypatch.delenv("LOGS_CHAT_ID", raising=False)

    return state


async def _run(pending_minutes=None, active_hours=None, awaiting_hold_minutes=None):
    return await reconcile.run(
        pending_minutes if pending_minutes is not None else reconcile.PENDING_STALE_MINUTES,
        active_hours if active_hours is not None else reconcile.ACTIVE_STALE_HOURS,
        awaiting_hold_minutes if awaiting_hold_minutes is not None else reconcile.AWAITING_HOLD_STALE_MINUTES,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# 1. 'pending' резервації (kWh)
# ---------------------------------------------------------------------------

async def test_stale_pending_reservation_gets_released_and_expired(billing):
    old_enough = NOW - timedelta(minutes=reconcile.PENDING_STALE_MINUTES + 5)
    billing.add_reservation(1, OPERATOR_A, user_id=777, reserved_kwh=Decimal("20.000"),
                            status="pending", created_at=old_enough)

    exit_code = await _run()

    assert billing.reservations[1]["status"] == "expired"
    assert billing.release_calls == [(1, "expired")]
    assert exit_code == 1, "Автозвільнення протухлого hold — сигнал моніторингу, не 'усе гаразд'"


async def test_pending_reservation_not_yet_old_enough_is_left_alone(billing):
    fresh = NOW - timedelta(minutes=5)
    billing.add_reservation(2, OPERATOR_A, user_id=777, reserved_kwh=Decimal("10.000"),
                            status="pending", created_at=fresh)

    exit_code = await _run()

    assert billing.reservations[2]["status"] == "pending"
    assert billing.release_calls == []
    assert exit_code == 0


# ---------------------------------------------------------------------------
# 2. 'active' резервації (kWh)
# ---------------------------------------------------------------------------

async def test_stale_active_reservation_gets_released_and_expired(billing):
    old_enough = NOW - timedelta(hours=reconcile.ACTIVE_STALE_HOURS + 1)
    billing.add_reservation(3, OPERATOR_A, user_id=777, reserved_kwh=Decimal("30.000"),
                            status="active", created_at=old_enough, updated_at=old_enough)

    exit_code = await _run()

    assert billing.reservations[3]["status"] == "expired"
    assert billing.release_calls == [(3, "expired")]
    assert exit_code == 1


async def test_active_reservation_updated_recently_is_left_alone(billing):
    """Звіряємо по updated_at, не created_at — свіжий activate_reservation() не застряглий."""
    long_ago_created = NOW - timedelta(hours=reconcile.ACTIVE_STALE_HOURS + 5)
    recent_update = NOW - timedelta(minutes=10)
    billing.add_reservation(4, OPERATOR_A, user_id=777, reserved_kwh=Decimal("15.000"),
                            status="active", created_at=long_ago_created, updated_at=recent_update)

    exit_code = await _run()

    assert billing.reservations[4]["status"] == "active"
    assert exit_code == 0


# ---------------------------------------------------------------------------
# 3. Race з паралельним StopTransaction, ідемпотентність (kWh)
# ---------------------------------------------------------------------------

async def test_race_with_parallel_stop_transaction_is_not_a_problem(billing, monkeypatch):
    """
    release_reservation_hold() повертає False (паралельний StopTransaction
    устиг фіналізувати резервацію першим, поки її обробляв reconcile) —
    не рахується проблемою, exit code лишається 0.
    """
    old_enough = NOW - timedelta(minutes=reconcile.PENDING_STALE_MINUTES + 5)
    billing.add_reservation(5, OPERATOR_A, user_id=777, reserved_kwh=Decimal("5.000"),
                            status="pending", created_at=old_enough)

    async def race_release(operator_id, reservation_id, new_status):
        return False

    monkeypatch.setattr(repo, "release_reservation_hold", race_release)

    exit_code = await _run()

    assert exit_code == 0
    assert billing.reservations[5]["status"] == "pending"


async def test_second_run_is_idempotent(billing):
    """Резервація вже 'expired' з першого прогону — другий прогін її не знаходить (не 'pending'/'active')."""
    old_enough = NOW - timedelta(minutes=reconcile.PENDING_STALE_MINUTES + 5)
    billing.add_reservation(6, OPERATOR_A, user_id=777, reserved_kwh=Decimal("8.000"),
                            status="pending", created_at=old_enough)

    first_exit = await _run()
    second_exit = await _run()

    assert first_exit == 1
    assert second_exit == 0
    assert billing.release_calls == [(6, "expired")]


# ---------------------------------------------------------------------------
# 4. Підсумок пуша в Telegram
# ---------------------------------------------------------------------------

async def test_summary_pushed_to_telegram_when_something_was_released(billing, monkeypatch):
    monkeypatch.setenv("LOGS_CHAT_ID", "-100999")
    sent = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(reconcile, "_get_bot", lambda: FakeBot())

    old_enough = NOW - timedelta(minutes=reconcile.PENDING_STALE_MINUTES + 5)
    billing.add_reservation(7, OPERATOR_A, user_id=777, reserved_kwh=Decimal("12.000"),
                            status="pending", created_at=old_enough)

    await _run()

    assert len(sent) == 1
    assert sent[0]["chat_id"] == "-100999"
    assert "Звірка kWh/грн-резервацій" in sent[0]["text"]


async def test_summary_not_pushed_when_nothing_was_released(billing, monkeypatch):
    monkeypatch.setenv("LOGS_CHAT_ID", "-100999")
    calls = []
    monkeypatch.setattr(reconcile, "_get_bot", lambda: calls.append(1))

    await _run()

    assert calls == []


async def test_telegram_push_failure_does_not_crash_reconciliation(billing, monkeypatch):
    """Збій пуша (бот недоступний, мережа тощо) не має ламати саму звірку."""
    monkeypatch.setenv("LOGS_CHAT_ID", "-100999")

    def broken_get_bot():
        raise RuntimeError("bot недоступний")

    monkeypatch.setattr(reconcile, "_get_bot", broken_get_bot)

    old_enough = NOW - timedelta(minutes=reconcile.PENDING_STALE_MINUTES + 5)
    billing.add_reservation(8, OPERATOR_A, user_id=777, reserved_kwh=Decimal("3.000"),
                            status="pending", created_at=old_enough)

    exit_code = await _run()

    assert exit_code == 1, "Звірка й досі мала завершитись і повернути правильний exit code"
    assert billing.reservations[8]["status"] == "expired"


# ---------------------------------------------------------------------------
# 5. Модель B, крок 1: 'awaiting_hold' застарілі
# ---------------------------------------------------------------------------

async def test_awaiting_hold_expired_when_bank_reports_terminal_failure(billing):
    """
    Банк дійшов до ЯВНО ТЕРМІНАЛЬНОГО 'failure' (оплата не пройшла) —
    безпечно позначаємо expired.

    Раніше цей тест звірявся на `bank_status="processing"` і сам кодував
    баг, який виправляє цей бандл: 'processing' — реальний ТРАНЗИТНИЙ стан
    на шляху до 'hold' (живий факт (з), SESSION_STATE.md, 2026-07-30), не
    термінальний — тепер це окремий сценарій, див.
    test_awaiting_hold_processing_and_created_are_transient_not_expired нижче.
    """
    old_enough = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES + 5)
    billing.add_uah_reservation(101, OPERATOR_A, "inv-101", Decimal("100.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.set_bank_status("inv-101", "failure")
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.reservations[101]["status"] == "expired"
    assert billing.cancel_calls == [], "Банк ніколи не тримав гроші — cancel_invoice не потрібен"
    assert exit_code == 1


async def test_awaiting_hold_expired_when_bank_reversed_after_own_auto_cancel(billing):
    """
    Знахідка рев'ю плану Opus: банк САМ автоскасував (напр. 9-денний
    таймаут hold, на який наш вебхук/алерт свого часу не відреагував) —
    'reversed' теж термінальний для 'awaiting_hold'-рядка. Без цієї гілки
    рядок випав би з видимості НАЗАВЖДИ: наступний прогін фільтрує лише
    статус 'awaiting_hold', а транзитна гілка (нижче) мовчки пропускала б
    його щоразу знову.
    """
    old_enough = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES + 5)
    billing.add_uah_reservation(106, OPERATOR_A, "inv-106", Decimal("100.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.set_bank_status("inv-106", "reversed")
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.reservations[106]["status"] == "expired"
    assert billing.cancel_calls == []
    assert exit_code == 1


async def test_awaiting_hold_alerts_when_bank_already_succeeded(billing):
    """
    Знахідка рев'ю плану Opus: банк уже СПИСАВ кошти (success), а рядок і
    досі 'awaiting_hold' — найгучніший можливий сценарій цього кроку. Не
    можна ні мовчки пропустити (гроші реально рухались), ні звести до
    'expired' (це приховало б сам факт списання) — лише алерт, рядок
    НЕДОТОРКАНИЙ, банк цим кроком нічого не мутує.
    """
    old_enough = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES + 5)
    billing.add_uah_reservation(107, OPERATOR_A, "inv-107", Decimal("100.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.set_bank_status("inv-107", "success", final_amount_kopecks=3500)
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.reservations[107]["status"] == "awaiting_hold", "Рядок має лишитись НЕДОТОРКАНИМ"
    assert billing.cancel_calls == []
    assert billing.finalize_calls == []
    assert exit_code == 1, "Алерт — теж сигнал моніторингу"


async def test_awaiting_hold_success_and_hold_alerts_have_distinct_reasons(billing):
    """
    'hold' і 'success' обидва йдуть у гілку алерту, але з РІЗНИМ reason:
    "банк тримає, RemoteStart не відбувся" — не те саме, що "банк уже
    списав кошти повз наш облік". Перевіряю напряму через stats (run()
    повертає лише exit_code, не сам об'єкт статистики).
    """
    cutoff = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES)
    old_enough = cutoff - timedelta(minutes=5)
    billing.add_uah_reservation(108, OPERATOR_A, "inv-108-hold", Decimal("100.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.add_uah_reservation(109, OPERATOR_A, "inv-109-success", Decimal("100.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.set_bank_status("inv-108-hold", "hold")
    billing.set_bank_status("inv-109-success", "success", final_amount_kopecks=3500)
    billing.set_operator_token(OPERATOR_A)

    stats = reconcile.ReconcileStats()
    await reconcile._reconcile_stale_awaiting_hold(cutoff, stats)

    reasons = {a["reservation_id"]: a["reason"] for a in stats.alerts}
    assert len(stats.alerts) == 2
    assert reasons[108] != reasons[109]
    assert "success" in reasons[109] or "спис" in reasons[109]


async def test_awaiting_hold_processing_and_created_are_transient_not_expired(billing):
    """
    ГОЛОВНА ГАРАНТІЯ правки 1 (живий факт (з), SESSION_STATE.md 2026-07-30):
    'processing'/'created' — реальний транзитний стан НА ШЛЯХУ до 'hold',
    не глухий кут. Раніше звірка позначала такий рядок 'expired' одразу —
    якщо оплата водія доходила до 'hold' вже ПІСЛЯ цього, вебхук шукав
    рядок за статусом 'awaiting_hold' і вже не знаходив його: гроші водія
    висіли б до 9-денного автоскасування банку без жодного нашого сліду.
    Тепер рядок лишається недоторканим, лише лічильник для видимості.
    """
    cutoff = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES)
    old_enough = cutoff - timedelta(minutes=5)
    billing.add_uah_reservation(110, OPERATOR_A, "inv-110-processing", Decimal("100.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.add_uah_reservation(111, OPERATOR_A, "inv-111-created", Decimal("50.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.set_bank_status("inv-110-processing", "processing")
    billing.set_bank_status("inv-111-created", "created")
    billing.set_operator_token(OPERATOR_A)

    stats = reconcile.ReconcileStats()
    await reconcile._reconcile_stale_awaiting_hold(cutoff, stats)

    assert billing.reservations[110]["status"] == "awaiting_hold"
    assert billing.reservations[111]["status"] == "awaiting_hold"
    assert billing.cancel_calls == []
    assert billing.finalize_calls == []
    assert stats.released == []
    assert stats.alerts == []
    assert stats.awaiting_hold_transient == 2


async def test_awaiting_hold_transient_state_does_not_affect_exit_code(billing):
    """
    Транзитний стан — НЕ сигнал моніторингу (на відміну від alerts/
    released): прогін, де це єдина знахідка, має завершитись exit_code 0.
    """
    old_enough = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES + 5)
    billing.add_uah_reservation(112, OPERATOR_A, "inv-112", Decimal("100.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.set_bank_status("inv-112", "processing")
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.reservations[112]["status"] == "awaiting_hold"
    assert exit_code == 0


async def test_awaiting_hold_only_alerts_when_bank_confirms_hold_never_touches_ocpp(billing):
    """
    ГОЛОВНА ГАРАНТІЯ кроку 1 (правка 4, раунд 3): банк підтвердив hold, а
    RemoteStart не відбувся — звірка НЕ чіпає рядок і НЕ намагається сама
    стартувати зарядку (окремий процес, _active_charge_points порожній) —
    лише алерт.
    """
    old_enough = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES + 5)
    billing.add_uah_reservation(102, OPERATOR_A, "inv-102", Decimal("100.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.set_bank_status("inv-102", "hold")
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.reservations[102]["status"] == "awaiting_hold", "Рядок має лишитись НЕДОТОРКАНИМ"
    assert billing.cancel_calls == []
    assert billing.finalize_calls == []
    assert exit_code == 1, "Алерт — теж сигнал моніторингу"


async def test_awaiting_hold_not_yet_old_enough_is_left_alone(billing):
    fresh = NOW - timedelta(minutes=5)
    billing.add_uah_reservation(103, OPERATOR_A, "inv-103", Decimal("50.00"),
                                status="awaiting_hold", created_at=fresh)
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.reservations[103]["status"] == "awaiting_hold"
    assert exit_code == 0


async def test_awaiting_hold_skipped_without_crashing_when_no_operator_token(billing):
    old_enough = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES + 5)
    billing.add_uah_reservation(104, OPERATOR_A, "inv-104", Decimal("50.00"),
                                status="awaiting_hold", created_at=old_enough)
    # НЕ викликаємо set_operator_token — токена немає.

    exit_code = await _run()

    assert billing.reservations[104]["status"] == "awaiting_hold"
    assert exit_code == 0


async def test_awaiting_hold_bank_unavailable_does_not_crash_or_touch_reservation(billing, monkeypatch):
    old_enough = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES + 5)
    billing.add_uah_reservation(105, OPERATOR_A, "inv-105", Decimal("50.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.set_operator_token(OPERATOR_A)

    async def broken_get_invoice_status(token, invoice_id):
        raise MonobankError("bank timeout")

    monkeypatch.setattr(reconcile, "get_invoice_status", broken_get_invoice_status)

    exit_code = await _run()

    assert billing.reservations[105]["status"] == "awaiting_hold"
    assert exit_code == 0


# ---------------------------------------------------------------------------
# 6. Модель B, крок 2: 'pending' застарілі (uah)
# ---------------------------------------------------------------------------

async def test_stale_pending_uah_cancels_hold_when_bank_still_holds(billing):
    old_enough = NOW - timedelta(minutes=reconcile.PENDING_STALE_MINUTES + 5)
    billing.add_uah_reservation(201, OPERATOR_A, "inv-201", Decimal("100.00"),
                                status="pending", created_at=old_enough)
    billing.set_bank_status("inv-201", "hold")
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.cancel_calls == ["inv-201"]
    assert billing.reservations[201]["status"] == "expired"
    assert billing.reservations[201]["final_amount_uah"] == Decimal("0")
    assert exit_code == 1


async def test_stale_pending_uah_does_not_recall_bank_when_already_success(billing):
    """Банк уже сам дійшов до success (хтось інший устиг) — НЕ дзвонимо повторно."""
    old_enough = NOW - timedelta(minutes=reconcile.PENDING_STALE_MINUTES + 5)
    billing.add_uah_reservation(202, OPERATOR_A, "inv-202", Decimal("100.00"),
                                status="pending", created_at=old_enough)
    billing.set_bank_status("inv-202", "success", final_amount_kopecks=5000)
    billing.set_operator_token(OPERATOR_A)

    await _run()

    assert billing.cancel_calls == []
    assert billing.finalize_calls == []
    assert billing.reservations[202]["status"] == "finalized"
    assert billing.reservations[202]["final_amount_uah"] == Decimal("50.00")


async def test_stale_pending_uah_syncs_expired_when_bank_already_reversed(billing):
    old_enough = NOW - timedelta(minutes=reconcile.PENDING_STALE_MINUTES + 5)
    billing.add_uah_reservation(203, OPERATOR_A, "inv-203", Decimal("100.00"),
                                status="pending", created_at=old_enough)
    billing.set_bank_status("inv-203", "reversed")
    billing.set_operator_token(OPERATOR_A)

    await _run()

    assert billing.cancel_calls == [], "Банк уже сам reversed — повторний cancel не потрібен"
    assert billing.reservations[203]["status"] == "expired"


async def test_stale_pending_uah_skips_bank_call_when_claim_lost(billing):
    """
    Другий блокер рев'ю: паралельний StartTransaction активував резервацію
    (pending -> active) між list_stale_pending_uah_reservations() і спробою
    заявки на розрахунок — claim_reservation_for_settlement() програє (0
    рядків, бо статус уже не 'pending'). Звірка НЕ повинна дзвонити в банк
    (cancel_invoice) у цьому випадку — живий шлях сам розбереться.
    """
    old_enough = NOW - timedelta(minutes=reconcile.PENDING_STALE_MINUTES + 5)
    billing.add_uah_reservation(210, OPERATOR_A, "inv-210", Decimal("100.00"),
                                status="pending", created_at=old_enough)
    billing.set_bank_status("inv-210", "hold")
    billing.set_operator_token(OPERATOR_A)
    billing.force_claim_loss(210)

    exit_code = await _run()

    assert (210, "pending") in billing.claim_calls, "claim мав бути СПРОБУВАНИЙ"
    assert billing.cancel_calls == [], "заявку програно — cancel_invoice НЕ мав викликатись"
    assert billing.reservations[210]["status"] == "pending"
    assert exit_code == 0


# ---------------------------------------------------------------------------
# 7. Модель B, крок 3 (БЛОКЕР рев'ю — закрито): 'active' uah застарілі
# ---------------------------------------------------------------------------

async def test_stale_active_uah_finalizes_when_session_already_completed(billing):
    """
    Крах МІЖ complete_ocpp_transaction() (сесія вже 'completed') і
    claim_reservation_for_settlement() (резервація ще НЕ 'settling') —
    факт спожитого відомий із сесії, звірка сама рахує й фіналізує.
    """
    old_enough = NOW - timedelta(hours=reconcile.ACTIVE_STALE_HOURS + 1)
    billing.add_uah_reservation(501, OPERATOR_A, "inv-501", Decimal("100.00"),
                                status="active", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=601, station_id=10)
    billing.add_session(601, OPERATOR_A, kwh=Decimal("5.000"), status="completed")
    billing.add_station(10, OPERATOR_A, tariff_uah_kwh=Decimal("10.00"), tariff_uah_start=Decimal("5.00"))
    billing.set_bank_status("inv-501", "hold")
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    # 5.00 + 10.00 * 5.000 = 55.00
    assert billing.finalize_calls == [("inv-501", Decimal("55.00"))]
    assert billing.cancel_calls == []
    assert billing.reservations[501]["status"] == "finalized"
    assert billing.reservations[501]["final_amount_uah"] == Decimal("55.00")
    assert exit_code == 1


async def test_stale_active_uah_cancels_in_full_when_stop_transaction_never_arrived(billing):
    """
    ГОЛОВНИЙ СЦЕНАРІЙ БЛОКЕРА: StopTransaction НІКОЛИ не прийшов (станція
    впала/водій висмикнув кабель) — сесії немає взагалі (чи вона не
    'completed'). Факт спожитого невідомий — НЕ вигадуємо, звільняємо
    ПОВНИЙ hold (той самий принцип, що kWh-модель при kwh=None).
    """
    old_enough = NOW - timedelta(hours=reconcile.ACTIVE_STALE_HOURS + 1)
    billing.add_uah_reservation(502, OPERATOR_A, "inv-502", Decimal("100.00"),
                                status="active", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=602, station_id=10)
    # Сесія НЕ 'completed' — StopTransaction не прийшов.
    billing.add_session(602, OPERATOR_A, kwh=None, status="charging")
    billing.set_bank_status("inv-502", "hold")
    billing.set_operator_token(OPERATOR_A)

    await _run()

    assert billing.cancel_calls == ["inv-502"]
    assert billing.finalize_calls == []
    assert billing.reservations[502]["status"] == "expired"
    assert billing.reservations[502]["final_amount_uah"] == Decimal("0")


async def test_stale_active_uah_cancels_in_full_when_no_session_bound_at_all(billing):
    """Той самий головний сценарій, але резервація взагалі без operator_session_id."""
    old_enough = NOW - timedelta(hours=reconcile.ACTIVE_STALE_HOURS + 1)
    billing.add_uah_reservation(503, OPERATOR_A, "inv-503", Decimal("50.00"),
                                status="active", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=None, station_id=10)
    billing.set_bank_status("inv-503", "hold")
    billing.set_operator_token(OPERATOR_A)

    await _run()

    assert billing.cancel_calls == ["inv-503"]
    assert billing.reservations[503]["status"] == "expired"


async def test_stale_active_uah_syncs_without_recalling_bank_when_already_success(billing):
    old_enough = NOW - timedelta(hours=reconcile.ACTIVE_STALE_HOURS + 1)
    billing.add_uah_reservation(504, OPERATOR_A, "inv-504", Decimal("100.00"),
                                status="active", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=604, station_id=10)
    billing.set_bank_status("inv-504", "success", final_amount_kopecks=4000)
    billing.set_operator_token(OPERATOR_A)
    # Свідомо НЕ додаємо сесію — не мала б знадобитись, банк уже 'success'.

    await _run()

    assert billing.finalize_calls == []
    assert billing.cancel_calls == []
    assert billing.reservations[504]["status"] == "finalized"
    assert billing.reservations[504]["final_amount_uah"] == Decimal("40.00")


async def test_stale_active_uah_not_yet_old_enough_is_left_alone(billing):
    fresh = NOW - timedelta(hours=1)
    billing.add_uah_reservation(505, OPERATOR_A, "inv-505", Decimal("100.00"),
                                status="active", created_at=fresh, updated_at=fresh,
                                operator_session_id=605, station_id=10)
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.reservations[505]["status"] == "active"
    assert billing.cancel_calls == []
    assert exit_code == 0


async def test_stale_active_uah_appears_in_telegram_summary(billing, monkeypatch):
    monkeypatch.setenv("LOGS_CHAT_ID", "-100999")
    sent = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(reconcile, "_get_bot", lambda: FakeBot())

    old_enough = NOW - timedelta(hours=reconcile.ACTIVE_STALE_HOURS + 1)
    billing.add_uah_reservation(506, OPERATOR_A, "inv-506", Decimal("100.00"),
                                status="active", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=None, station_id=10)
    billing.set_bank_status("inv-506", "hold")
    billing.set_operator_token(OPERATOR_A)

    await _run()

    assert len(sent) == 1
    assert "stale_active_uah" in sent[0]["text"]


async def test_stale_active_uah_skips_bank_call_when_claim_lost(billing):
    """
    Другий блокер рев'ю, ГОЛОВНИЙ сценарій: станція повертається з офлайну
    й шле накопичений StopTransaction — живий шлях (on_stop_transaction ->
    complete_ocpp_transaction_and_release_uah) устигає заклеймити
    (active -> settling) і сам дзвонить у банк ПАРАЛЕЛЬНО зі звіркою, що
    прочитала той самий рядок як ще 'active'. claim_reservation_for_
    settlement() у звірці програє — вона НЕ повинна дзвонити в банк вдруге
    (інакше подвійне списання з картки водія).
    """
    old_enough = NOW - timedelta(hours=reconcile.ACTIVE_STALE_HOURS + 1)
    billing.add_uah_reservation(510, OPERATOR_A, "inv-510", Decimal("100.00"),
                                status="active", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=610, station_id=10)
    billing.add_session(610, OPERATOR_A, kwh=Decimal("5.000"), status="completed")
    billing.add_station(10, OPERATOR_A, tariff_uah_kwh=Decimal("10.00"), tariff_uah_start=Decimal("5.00"))
    billing.set_bank_status("inv-510", "hold")
    billing.set_operator_token(OPERATOR_A)
    billing.force_claim_loss(510)

    exit_code = await _run()

    assert (510, "active") in billing.claim_calls, "claim мав бути СПРОБУВАНИЙ"
    assert billing.finalize_calls == [], "заявку програно — finalize_invoice НЕ мав викликатись"
    assert billing.cancel_calls == [], "заявку програно — cancel_invoice НЕ мав викликатись"
    assert billing.reservations[510]["status"] == "active"
    assert exit_code == 0


# ---------------------------------------------------------------------------
# 8. Модель B, крок 4: 'settling' застарілі
# ---------------------------------------------------------------------------

async def test_settling_finalizes_using_shared_pure_cost_function(billing):
    """
    Банк усе ще 'hold' — звірка сама рахує вартість compute_uah_settlement_
    amount() із kwh уже completed сесії й тарифу станції, і фіналізує.
    """
    old_enough = NOW - timedelta(seconds=reconcile.SETTLING_STALE_SECONDS + 30)
    billing.add_uah_reservation(301, OPERATOR_A, "inv-301", Decimal("100.00"),
                                status="settling", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=555, station_id=10)
    billing.add_session(555, OPERATOR_A, kwh=Decimal("5.000"))
    billing.add_station(10, OPERATOR_A, tariff_uah_kwh=Decimal("10.00"), tariff_uah_start=Decimal("5.00"))
    billing.set_bank_status("inv-301", "hold")
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    # 5.00 (старт) + 10.00 * 5.000 (тариф * кВт·год) = 55.00
    assert billing.finalize_calls == [("inv-301", Decimal("55.00"))]
    assert billing.cancel_calls == []
    assert billing.reservations[301]["status"] == "finalized"
    assert billing.reservations[301]["final_amount_uah"] == Decimal("55.00")
    assert exit_code == 1


async def test_settling_caps_cost_at_reserved_amount(billing):
    """Перевитрата (Q4/варіант 1) — фіналізуємо капованою утриманою сумою, не фактом понад неї."""
    old_enough = NOW - timedelta(seconds=reconcile.SETTLING_STALE_SECONDS + 30)
    billing.add_uah_reservation(302, OPERATOR_A, "inv-302", Decimal("20.00"),
                                status="settling", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=556, station_id=10)
    billing.add_session(556, OPERATOR_A, kwh=Decimal("50.000"))  # свідомо величезний факт
    billing.add_station(10, OPERATOR_A, tariff_uah_kwh=Decimal("10.00"), tariff_uah_start=None)
    billing.set_bank_status("inv-302", "hold")
    billing.set_operator_token(OPERATOR_A)

    await _run()

    assert billing.finalize_calls == [("inv-302", Decimal("20.00"))], "Капується на утримане, не 500.00"


async def test_settling_cancels_when_kwh_is_none(billing):
    """kwh=None (абсурдна дельта лічильника, 3b) -> вартість 0 -> cancel_invoice(), не finalize()."""
    old_enough = NOW - timedelta(seconds=reconcile.SETTLING_STALE_SECONDS + 30)
    billing.add_uah_reservation(303, OPERATOR_A, "inv-303", Decimal("20.00"),
                                status="settling", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=557, station_id=10)
    billing.add_session(557, OPERATOR_A, kwh=None)
    billing.add_station(10, OPERATOR_A, tariff_uah_kwh=Decimal("10.00"))
    billing.set_bank_status("inv-303", "hold")
    billing.set_operator_token(OPERATOR_A)

    await _run()

    assert billing.cancel_calls == ["inv-303"]
    assert billing.finalize_calls == []
    assert billing.reservations[303]["final_amount_uah"] == Decimal("0.00")


async def test_settling_syncs_without_recalling_bank_when_already_success(billing):
    """Наш дзвінок у банк відбувся, а локальний запис — ні (крах МІЖ ними) — лише синхронізуємо."""
    old_enough = NOW - timedelta(seconds=reconcile.SETTLING_STALE_SECONDS + 30)
    billing.add_uah_reservation(304, OPERATOR_A, "inv-304", Decimal("100.00"),
                                status="settling", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=558, station_id=10)
    billing.set_bank_status("inv-304", "success", final_amount_kopecks=3000)
    billing.set_operator_token(OPERATOR_A)
    # Свідомо НЕ додаємо сесію/станцію — не мали б знадобитись, бо банк уже 'success'.

    await _run()

    assert billing.finalize_calls == [], "Банк уже фіналізував — повторний виклик не потрібен"
    assert billing.reservations[304]["status"] == "finalized"
    assert billing.reservations[304]["final_amount_uah"] == Decimal("30.00")


async def test_settling_unexpected_bank_status_recorded_in_alerts(billing):
    """
    Знахідка живого смоуку 2026-07-30: раніше неочікуваний стан банку тут
    писав лише logger.error — не потрапляв ні в stats.alerts, ні в
    Telegram-підсумок, ні в exit code. Той самий клас невидимості, що вже
    задокументоване капування перевитрати (SESSION_STATE.md). Перевіряю
    напряму через stats — run() повертає лише exit_code.
    """
    old_enough = NOW - timedelta(seconds=reconcile.SETTLING_STALE_SECONDS + 30)
    billing.add_uah_reservation(306, OPERATOR_A, "inv-306", Decimal("100.00"),
                                status="settling", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=559, station_id=10)
    billing.set_bank_status("inv-306", "processing")  # не мав би тут з'явитись, але код не виключає
    billing.set_operator_token(OPERATOR_A)

    stats = reconcile.ReconcileStats()
    cutoff = NOW - timedelta(seconds=reconcile.SETTLING_STALE_SECONDS)
    await reconcile._reconcile_stale_settling(cutoff, stats)

    assert billing.reservations[306]["status"] == "settling", "Рядок недоторканий — не вигадуємо суму"
    assert billing.finalize_calls == []
    assert billing.cancel_calls == []
    assert stats.released == []
    assert len(stats.alerts) == 1
    assert stats.alerts[0]["reservation_id"] == 306
    assert stats.alerts[0]["invoice_id"] == "inv-306"
    assert "processing" in stats.alerts[0]["reason"]


async def test_settling_unexpected_bank_status_yields_exit_code_one(billing):
    """Той самий сценарій через повний run() — контракт cron/exit code, не лише внутрішній stats."""
    old_enough = NOW - timedelta(seconds=reconcile.SETTLING_STALE_SECONDS + 30)
    billing.add_uah_reservation(307, OPERATOR_A, "inv-307", Decimal("100.00"),
                                status="settling", created_at=old_enough, updated_at=old_enough,
                                operator_session_id=560, station_id=10)
    billing.set_bank_status("inv-307", "processing")
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.reservations[307]["status"] == "settling"
    assert exit_code == 1


async def test_settling_not_yet_old_enough_is_left_alone(billing):
    fresh = NOW - timedelta(seconds=10)
    billing.add_uah_reservation(305, OPERATOR_A, "inv-305", Decimal("100.00"),
                                status="settling", created_at=fresh, updated_at=fresh)
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert billing.reservations[305]["status"] == "settling"
    assert exit_code == 0


def test_settling_stale_seconds_is_safely_larger_than_bank_http_timeout():
    """
    Правка 7 рев'ю: SETTLING_STALE_SECONDS МУСИТЬ бути свідомо більшим за
    monobank_acquiring.DEFAULT_TIMEOUT — інакше звірка може втрутитися в
    рядок 'settling', поки живий виклик finalize/cancel ще фізично
    виконується в мережі. Сам модуль уже це стверджує через assert при
    імпорті — цей тест лише фіксує властивість явно, як регресію.
    """
    from app.services.monobank_acquiring import DEFAULT_TIMEOUT
    assert reconcile.SETTLING_STALE_SECONDS > DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# 9. Підсумок алертів (крок 1) у Telegram
# ---------------------------------------------------------------------------

async def test_alerts_are_included_in_telegram_summary(billing, monkeypatch):
    monkeypatch.setenv("LOGS_CHAT_ID", "-100999")
    sent = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(reconcile, "_get_bot", lambda: FakeBot())

    old_enough = NOW - timedelta(minutes=reconcile.AWAITING_HOLD_STALE_MINUTES + 5)
    billing.add_uah_reservation(401, OPERATOR_A, "inv-401", Decimal("100.00"),
                                status="awaiting_hold", created_at=old_enough)
    billing.set_bank_status("inv-401", "hold")
    billing.set_operator_token(OPERATOR_A)

    exit_code = await _run()

    assert exit_code == 1
    assert len(sent) == 1
    assert "РУЧНОГО розбору" in sent[0]["text"]
    assert "inv-401" in sent[0]["text"]


# ---------------------------------------------------------------------------
# 10. Формулювання підсумку (_format_line) — правка 3
# ---------------------------------------------------------------------------

def test_format_line_stale_awaiting_hold_says_bank_never_held_money():
    """
    Знахідка живого смоуку 2026-07-30: "звільнено/розраховано" описувало б
    дію, якої не було — банк тут НІКОЛИ не тримав гроші. Окреме чесне
    формулювання, із сумою (рев'ю плану Opus).
    """
    line = reconcile._format_line({
        "type": "stale_awaiting_hold", "reservation_id": 101,
        "operator_id": OPERATOR_A, "reserved_uah": Decimal("100.00"),
    })
    assert "протух неоплаченим" in line
    assert "банк коштів не утримував" in line
    assert "100.00" in line
    assert "звільнено/розраховано" not in line


def test_format_line_other_types_unchanged():
    """Регресія: решта типів (kwh і uah, де гроші/кВт·год дійсно рухались) форматуються як раніше."""
    kwh_line = reconcile._format_line({
        "type": "stale_pending", "reservation_id": 1, "operator_id": OPERATOR_A,
        "user_id": 777, "reserved_kwh": Decimal("20.000"),
    })
    assert "звільнено/розраховано 20.000 кВт·год" in kwh_line

    uah_line = reconcile._format_line({
        "type": "stale_settling", "reservation_id": 301, "operator_id": OPERATOR_A,
        "reserved_uah": Decimal("100.00"),
    })
    assert "звільнено/розраховано 100.00 грн" in uah_line
