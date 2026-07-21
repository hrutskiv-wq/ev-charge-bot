"""Тести app/services/geo.py::haversine_km — чиста функція, без БД і мережі."""
import pytest

from app.services.geo import haversine_km


def test_distance_between_identical_points_is_zero():
    assert haversine_km(49.8397, 24.0297, 49.8397, 24.0297) == pytest.approx(0.0, abs=1e-9)


def test_distance_lviv_to_kyiv_is_roughly_correct():
    # Львів -> Київ: приблизно 470 км по прямій.
    lviv = (49.8397, 24.0297)
    kyiv = (50.4501, 30.5234)
    distance = haversine_km(*lviv, *kyiv)
    assert 460 <= distance <= 480


def test_distance_is_symmetric():
    a = (49.8397, 24.0297)
    b = (49.9035, 24.1097)
    assert haversine_km(*a, *b) == pytest.approx(haversine_km(*b, *a))


def test_short_distance_within_a_city_is_a_few_kilometers():
    center = (49.8397, 24.0297)
    nearby = (49.9035, 24.1097)  # приблизно в іншому районі того ж міста
    distance = haversine_km(*center, *nearby)
    assert 0 < distance < 15
