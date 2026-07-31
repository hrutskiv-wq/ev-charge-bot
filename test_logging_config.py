"""
Тест на конфігурацію логера httpx (app/main.py) — фікс-бандл після живого
смоуку TomTom 31.07.2026: httpx на рівні INFO друкує повний URL кожного
запиту, включно з query-параметрами, а TomTom приймає ключ саме в query
(?key=...) — без приглушення він лежав відкритим текстом у логах
контейнера при кожному пошуку станцій.

Той самий підхід, що test_health.py: `import app.main as main_module` —
модульний рівень файлу виконується один раз при імпорті, `logging.
getLogger("httpx").setLevel(...)` там же.

Запуск: pytest test_logging_config.py -v
"""
import logging

import app.main as main_module  # noqa: F401 — сам факт імпорту виконує logging.basicConfig/setLevel


def test_httpx_logger_level_is_warning():
    assert logging.getLogger("httpx").level == logging.WARNING


def test_httpx_logger_effective_level_suppresses_info():
    """Не лише .level виставлено — INFO-рівневі записи httpx реально не
    проходять через getEffectiveLevel(), яке й перевіряє logging при
    виклику logger.info(...)."""
    assert logging.getLogger("httpx").getEffectiveLevel() > logging.INFO
