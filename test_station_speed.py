"""
Тести app/services/station_speed.py::classify_station_speed — чиста функція,
однакова для операторських станцій і станцій OCM (Промпт 4c).
"""
import pytest

from app.services.station_speed import (
    BADGE_FAST, BADGE_MEDIUM, BADGE_SLOW, classify_station_speed,
)


@pytest.mark.parametrize("power_kw,expected", [
    (50, BADGE_FAST),
    (60, BADGE_FAST),
    (150, BADGE_FAST),
    (49.9, BADGE_MEDIUM),
    (30, BADGE_MEDIUM),
    (22.1, BADGE_MEDIUM),
    (22, BADGE_SLOW),          # межа: рівно 22 — повільна, не середня
    (10, BADGE_SLOW),
    (0, BADGE_SLOW),
])
def test_classify_by_power_thresholds(power_kw, expected):
    assert classify_station_speed(power_kw, connector_type=None) == expected


def test_power_as_string_or_decimal_is_handled():
    from decimal import Decimal
    assert classify_station_speed("60", None) == BADGE_FAST
    assert classify_station_speed(Decimal("22"), None) == BADGE_SLOW


def test_power_known_ignores_connector_hints():
    """Якщо потужність відома — рішення лише за нею, конектор не перебиває."""
    assert classify_station_speed(10, "CCS") == BADGE_SLOW
    assert classify_station_speed(60, "Schuko") == BADGE_FAST


@pytest.mark.parametrize("connector_type,expected", [
    ("CCS", BADGE_FAST),
    ("CCS (Type 2)", BADGE_FAST),
    ("CHAdeMO", BADGE_FAST),
    ("GB/T DC", BADGE_FAST),
    ("ccs", BADGE_FAST),  # регістр не має значення
    ("Schuko", BADGE_SLOW),
    ("CEE 5-pin (3ф)", BADGE_SLOW),
    ("розетка побутова", BADGE_SLOW),
])
def test_classify_by_connector_when_power_unknown(connector_type, expected):
    assert classify_station_speed(None, connector_type) == expected


def test_gbt_ac_is_not_confused_with_gbt_dc():
    """GB/T AC — повільний/середній побутовий варіант, не швидкий DC-хінт GB/T DC."""
    assert classify_station_speed(None, "GB/T AC") is None


@pytest.mark.parametrize("power_kw,connector_type", [
    (None, None),
    (None, "Type 2"),          # не в жодному списку хінтів
    (None, ""),
    ("не число", "Type 2"),
])
def test_returns_none_when_nothing_to_classify_from(power_kw, connector_type):
    assert classify_station_speed(power_kw, connector_type) is None
