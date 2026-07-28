"""
Тести на app/services/operator_activation.py — чек-лист прогресу й
автоактивація оператора (самообслуговуваний онбординг). Той самий
фейковий підхід, що в test_ocpp_charging_service.py: repo.* підмінені
напряму, жива БД не потрібна.

Ключова гарантія: автоактивація спрацьовує РІВНО тоді, коли всі три
критерії виконано (профіль/токен-підтверджений/станція), і НЕ раніше —
кожен неповний набір перевірено окремим тестом; ідемпотентність (другий
виклик на вже активному операторі — жодних змін) — окремим набором.

Запуск: pytest test_operator_activation.py -v
"""
from datetime import datetime, timezone

import pytest

from app.database import operators_repo as repo
from app.services import operator_activation as oa

OPERATOR_A = 1
VERIFIED_AT = datetime(2026, 7, 28, tzinfo=timezone.utc)


def _operator(status="pending", name="Готель Едем", phone="+380501234567",
             verified_at=None):
    return {
        "id": OPERATOR_A, "name": name, "phone": phone, "status": status,
        "monobank_token_verified_at": verified_at,
    }


def _patch_operator(monkeypatch, operator):
    async def fake_get_operator(operator_id):
        assert operator_id == OPERATOR_A
        return operator
    monkeypatch.setattr(repo, "get_operator", fake_get_operator)


def _patch_token(monkeypatch, encrypted):
    async def fake_get_token(operator_id):
        return encrypted
    monkeypatch.setattr(repo, "get_operator_monobank_token_encrypted", fake_get_token)


def _patch_stations(monkeypatch, stations):
    async def fake_list_stations(operator_id):
        return stations
    monkeypatch.setattr(repo, "list_stations", fake_list_stations)


# ---------------------------------------------------------------------------
# get_checklist()
# ---------------------------------------------------------------------------

async def test_checklist_all_criteria_met_is_ready(monkeypatch):
    _patch_operator(monkeypatch, _operator(verified_at=VERIFIED_AT))
    _patch_token(monkeypatch, "encrypted-token")
    _patch_stations(monkeypatch, [{"id": 10}])

    checklist = await oa.get_checklist(OPERATOR_A)

    assert checklist.profile_complete is True
    assert checklist.has_token is True
    assert checklist.token_verified is True
    assert checklist.has_station is True
    assert checklist.ready is True


async def test_checklist_missing_operator_is_not_ready(monkeypatch):
    _patch_operator(monkeypatch, None)

    checklist = await oa.get_checklist(OPERATOR_A)

    assert checklist.ready is False
    assert checklist == oa.OperatorChecklist(False, False, False, False)


async def test_checklist_no_token_at_all(monkeypatch):
    _patch_operator(monkeypatch, _operator())
    _patch_token(monkeypatch, None)
    _patch_stations(monkeypatch, [{"id": 10}])

    checklist = await oa.get_checklist(OPERATOR_A)

    assert checklist.has_token is False
    assert checklist.token_verified is False
    assert checklist.ready is False


async def test_checklist_token_saved_but_not_yet_verified(monkeypatch):
    """
    has_token True, token_verified False — розрізнення потрібне UI: "токена
    нема" (кнопка підключити) vs "є, але банк ще не підтвердив" (кнопка
    "перевірити ще раз", без повторного вводу).
    """
    _patch_operator(monkeypatch, _operator(verified_at=None))
    _patch_token(monkeypatch, "encrypted-token")
    _patch_stations(monkeypatch, [{"id": 10}])

    checklist = await oa.get_checklist(OPERATOR_A)

    assert checklist.has_token is True
    assert checklist.token_verified is False
    assert checklist.ready is False


async def test_checklist_no_stations(monkeypatch):
    _patch_operator(monkeypatch, _operator(verified_at=VERIFIED_AT))
    _patch_token(monkeypatch, "encrypted-token")
    _patch_stations(monkeypatch, [])

    checklist = await oa.get_checklist(OPERATOR_A)

    assert checklist.has_station is False
    assert checklist.ready is False


async def test_checklist_incomplete_profile(monkeypatch):
    """Гіпотетичний випадок (сьогодні онбординг завжди вимагає обидва поля) — перевірено явно."""
    _patch_operator(monkeypatch, _operator(phone=""))
    _patch_token(monkeypatch, "encrypted-token")
    _patch_stations(monkeypatch, [{"id": 10}])

    checklist = await oa.get_checklist(OPERATOR_A)

    assert checklist.profile_complete is False
    assert checklist.ready is False


# ---------------------------------------------------------------------------
# try_auto_activate() — рівно на повному наборі, не раніше
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verified_at,stations", [
    (None, [{"id": 10}]),      # токен не підтверджений
    (VERIFIED_AT, []),          # немає станції
])
async def test_try_auto_activate_does_not_activate_on_incomplete_checklist(
        monkeypatch, verified_at, stations):
    _patch_operator(monkeypatch, _operator(status="pending", verified_at=verified_at))
    _patch_token(monkeypatch, "encrypted-token")
    _patch_stations(monkeypatch, stations)

    activate_calls = []
    async def fake_activate(operator_id):
        activate_calls.append(operator_id)
        return True
    monkeypatch.setattr(repo, "activate_operator_if_pending", fake_activate)

    result = await oa.try_auto_activate(OPERATOR_A)

    assert result is False
    assert activate_calls == [], "Неповний чек-лист — activate_operator_if_pending не мав викликатись"


async def test_try_auto_activate_activates_when_all_three_criteria_met(monkeypatch):
    _patch_operator(monkeypatch, _operator(status="pending", verified_at=VERIFIED_AT))
    _patch_token(monkeypatch, "encrypted-token")
    _patch_stations(monkeypatch, [{"id": 10}])

    activate_calls = []
    async def fake_activate(operator_id):
        activate_calls.append(operator_id)
        return True
    monkeypatch.setattr(repo, "activate_operator_if_pending", fake_activate)

    result = await oa.try_auto_activate(OPERATOR_A)

    assert result is True
    assert activate_calls == [OPERATOR_A]


async def test_try_auto_activate_is_noop_when_operator_already_active(monkeypatch):
    """Ідемпотентність: оператор уже НЕ pending -> миттєвий False, чек-лист навіть не рахується."""
    _patch_operator(monkeypatch, _operator(status="active", verified_at=VERIFIED_AT))

    checklist_calls = []
    async def fake_get_token(operator_id):
        checklist_calls.append(operator_id)
        return "encrypted-token"
    monkeypatch.setattr(repo, "get_operator_monobank_token_encrypted", fake_get_token)

    activate_calls = []
    async def fake_activate(operator_id):
        activate_calls.append(operator_id)
        return True
    monkeypatch.setattr(repo, "activate_operator_if_pending", fake_activate)

    result = await oa.try_auto_activate(OPERATOR_A)

    assert result is False
    assert checklist_calls == [], "Оператор уже активний — чек-лист рахувати не було сенсу"
    assert activate_calls == []


async def test_try_auto_activate_is_noop_when_operator_suspended(monkeypatch):
    _patch_operator(monkeypatch, _operator(status="suspended", verified_at=VERIFIED_AT))

    activate_calls = []
    async def fake_activate(operator_id):
        activate_calls.append(operator_id)
        return True
    monkeypatch.setattr(repo, "activate_operator_if_pending", fake_activate)

    result = await oa.try_auto_activate(OPERATOR_A)

    assert result is False
    assert activate_calls == []


async def test_try_auto_activate_second_call_after_activation_is_noop(monkeypatch):
    """
    Реальна ідемпотентність наскрізно: перший виклик активує (репозиторій
    теж підмінений реалістично — імітує атомарний UPDATE ... WHERE
    status='pending'), другий на тому самому operator_id (тепер уже
    active в фейковому стані) — жодної повторної активації.
    """
    state = {"status": "pending"}

    async def fake_get_operator(operator_id):
        return _operator(status=state["status"], verified_at=VERIFIED_AT)
    monkeypatch.setattr(repo, "get_operator", fake_get_operator)
    _patch_token(monkeypatch, "encrypted-token")
    _patch_stations(monkeypatch, [{"id": 10}])

    activate_calls = []
    async def fake_activate(operator_id):
        if state["status"] != "pending":
            return False
        state["status"] = "active"
        activate_calls.append(operator_id)
        return True
    monkeypatch.setattr(repo, "activate_operator_if_pending", fake_activate)

    first = await oa.try_auto_activate(OPERATOR_A)
    second = await oa.try_auto_activate(OPERATOR_A)

    assert first is True
    assert second is False
    assert activate_calls == [OPERATOR_A], "Активація мала відбутись рівно один раз"
