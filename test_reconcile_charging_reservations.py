"""
Тести на reconcile_charging_reservations.py (Промпт 3c-i).

Той самий стиль, що й test_reconcile_operators.py: живої Postgres немає,
репозиторій підмінений фейком у пам'яті. Перевіряється: (1) пороги
pending/active правильно відсікають ще не застряглі резервації, (2)
звільнення йде через release_reservation_hold() (не напряму
update_user_balance — та атомарність уже перевірена в
test_charging_reservations.py), (3) race з паралельним StopTransaction
(release_reservation_hold повертає False) не рахується проблемою, (4)
ідемпотентність — другий прогін нічого вже не звільняє.

Запуск: pytest test_reconcile_charging_reservations.py -v
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import reconcile_charging_reservations as reconcile
from app.database import operators_repo as repo

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
OPERATOR_A = 1


class FakeReservationBilling:
    def __init__(self):
        self.reservations = {}
        self.release_calls = []  # [(reservation_id, new_status), ...]

    def add_reservation(self, reservation_id, operator_id, user_id, reserved_kwh,
                        status, created_at, updated_at=None):
        self.reservations[reservation_id] = {
            "id": reservation_id, "operator_id": operator_id, "station_id": 10,
            "user_id": user_id, "payment_method": "kwh", "reserved_kwh": reserved_kwh,
            "id_tag": f"tag-{reservation_id}", "status": status,
            "operator_session_id": 555 if status == "active" else None,
            "created_at": created_at, "updated_at": updated_at or created_at,
        }


@pytest.fixture
def billing(monkeypatch):
    state = FakeReservationBilling()

    async def list_stale_pending_reservations(older_than):
        return [dict(r) for r in state.reservations.values()
                if r["status"] == "pending" and r["created_at"] < older_than]

    async def list_stale_active_reservations(older_than):
        return [dict(r) for r in state.reservations.values()
                if r["status"] == "active" and r["updated_at"] < older_than]

    async def release_reservation_hold(operator_id, reservation_id, new_status):
        r = state.reservations.get(reservation_id)
        if r is None or r["operator_id"] != operator_id or r["status"] not in ("pending", "active"):
            return False
        r["status"] = new_status
        state.release_calls.append((reservation_id, new_status))
        return True

    for name, func in [
        ("list_stale_pending_reservations", list_stale_pending_reservations),
        ("list_stale_active_reservations", list_stale_active_reservations),
        ("release_reservation_hold", release_reservation_hold),
    ]:
        monkeypatch.setattr(repo, name, func)

    async def _noop():
        return None

    monkeypatch.setattr(reconcile, "init_postgres", _noop)
    monkeypatch.setattr(reconcile, "close_postgres", _noop)
    monkeypatch.delenv("LOGS_CHAT_ID", raising=False)

    return state


async def _run(pending_minutes=None, active_hours=None):
    return await reconcile.run(
        pending_minutes if pending_minutes is not None else reconcile.PENDING_STALE_MINUTES,
        active_hours if active_hours is not None else reconcile.ACTIVE_STALE_HOURS,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# 1. 'pending' резервації
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
# 2. 'active' резервації
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
# 3. Race з паралельним StopTransaction, ідемпотентність
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
    assert "Звірка kWh-резервацій" in sent[0]["text"]


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
