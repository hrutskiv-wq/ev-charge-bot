"""
Тести на GET /health (app/main.py) — перевірку живучості для
docker-compose (condition: service_healthy) і зовнішнього моніторингу.

TestClient створюється БЕЗ `with ... as client:`, тому lifespan (startup,
який реально піднімає Postgres-пул через init_postgres()) не виконується —
ми повністю мокаємо connection.db_pool і dp.storage.redis, живі Postgres/
Redis для цих тестів не потрібні.

Запуск: pytest test_health.py -v
"""
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import app.main as main_module


def _make_mock_pool(select_1_result=1):
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = select_1_result

    mock_acquire_cm = AsyncMock()
    mock_acquire_cm.__aenter__.return_value = mock_conn
    mock_acquire_cm.__aexit__.return_value = None

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=mock_acquire_cm)
    return mock_pool


def test_health_ok_when_postgres_and_redis_fine():
    mock_pool = _make_mock_pool()
    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True

    with patch.object(main_module.connection, "db_pool", mock_pool), \
         patch.object(main_module.dp.storage, "redis", mock_redis, create=True):
        client = TestClient(main_module.app)
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"


def test_health_503_when_db_pool_not_initialized():
    with patch.object(main_module.connection, "db_pool", None):
        client = TestClient(main_module.app)
        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"].startswith("error:")


def test_health_503_when_redis_ping_fails():
    mock_pool = _make_mock_pool()
    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = ConnectionError("боляче")

    with patch.object(main_module.connection, "db_pool", mock_pool), \
         patch.object(main_module.dp.storage, "redis", mock_redis, create=True):
        client = TestClient(main_module.app)
        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["postgres"] == "ok"  # postgres сам по собі ок
    assert body["checks"]["redis"].startswith("error:")


def test_health_ok_without_redis_configured():
    """Якщо FSM працює на MemoryStorage (без Redis) — це не має вважатись
    "нездоровим" станом, лише постгрес критичний."""
    mock_pool = _make_mock_pool()

    with patch.object(main_module.connection, "db_pool", mock_pool):
        # Явно прибираємо атрибут redis, якщо він раптом лишився від
        # попереднього тесту (MemoryStorage його взагалі не має).
        if hasattr(main_module.dp.storage, "redis"):
            delattr(main_module.dp.storage, "redis")
        client = TestClient(main_module.app)
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["redis"] == "not configured (MemoryStorage)"
