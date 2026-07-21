"""
Класифікація швидкості зарядної станції (Промпт 4c) — чиста функція,
однакова для операторських станцій (White-Label білінг) і станцій OCM.

Пріоритет: якщо потужність відома — рішення ЛИШЕ за нею (пороги нижче).
Конектор дивимось тільки тоді, коли потужності немає взагалі — деякі типи
конекторів (CCS/CHAdeMO/GB/T DC — завжди швидка DC-зарядка; Schuko/CEE/
розетка — завжди повільна побутова AC) самі по собі є сильним сигналом.
Якщо немає ні потужності, ні розпізнаного конектора — бейджа не показуємо,
а не вгадуємо.
"""

BADGE_FAST = "⚡ Швидка (DC)"
BADGE_MEDIUM = "🔌 Середня (AC)"
BADGE_SLOW = "🐢 Повільна (AC)"

FAST_POWER_THRESHOLD_KW = 50
SLOW_POWER_THRESHOLD_KW = 22  # <= цього — повільна; строго вище і < FAST — середня

FAST_CONNECTOR_HINTS = ("CCS", "CHAdeMO", "GB/T DC")
SLOW_CONNECTOR_HINTS = ("Schuko", "CEE", "розетка")


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_station_speed(power_kw, connector_type: str = None):
    """Повертає бейдж-рядок або None, якщо класифікувати нема з чого."""
    power = _to_float(power_kw)
    if power is not None:
        if power >= FAST_POWER_THRESHOLD_KW:
            return BADGE_FAST
        if power > SLOW_POWER_THRESHOLD_KW:
            return BADGE_MEDIUM
        return BADGE_SLOW

    if connector_type:
        lowered = connector_type.lower()
        if any(hint.lower() in lowered for hint in FAST_CONNECTOR_HINTS):
            return BADGE_FAST
        if any(hint.lower() in lowered for hint in SLOW_CONNECTOR_HINTS):
            return BADGE_SLOW

    return None
