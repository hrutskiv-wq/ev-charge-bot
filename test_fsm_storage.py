"""
Тести на вибір FSM-сховища в app/core/loader.py: RedisStorage, коли заданий
REDIS_HOST (щоб стан діалогу переживав рестарт контейнера), MemoryStorage —
коли ні (локальна розробка без Redis). Перевіряються самим імпортом модуля
з різними env-змінними (логіка вибору виконується один раз на рівні модуля).

Запуск: pytest test_fsm_storage.py -v
"""
import importlib
import sys

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage


def _reload_loader_with_env(monkeypatch, redis_host):
    monkeypatch.setenv("BOT_TOKEN", "123456:test-bot-token-for-pytest")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.delenv("LOGS_CHAT_ID", raising=False)
    if redis_host is None:
        monkeypatch.delenv("REDIS_HOST", raising=False)
    else:
        monkeypatch.setenv("REDIS_HOST", redis_host)

    sys.modules.pop("app.core.loader", None)
    return importlib.import_module("app.core.loader")


def test_uses_redis_storage_when_redis_host_set(monkeypatch):
    loader = _reload_loader_with_env(monkeypatch, redis_host="redis")
    assert isinstance(loader.dp.storage, RedisStorage)


def test_falls_back_to_memory_storage_without_redis_host(monkeypatch):
    loader = _reload_loader_with_env(monkeypatch, redis_host=None)
    assert isinstance(loader.dp.storage, MemoryStorage)
