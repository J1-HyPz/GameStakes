"""Health endpoint and app shell behaviour."""

import pytest
from fastapi.testclient import TestClient


def test_health_reports_ok_with_sqlite_and_no_redis(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "GameStakes"
    assert body["database"]["status"] == "up"
    assert body["redis"]["status"] == "disabled"
    assert body["providers"] == []


def test_request_id_header_is_set(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.headers["X-Request-ID"]


def test_request_id_is_propagated_when_supplied(client: TestClient) -> None:
    response = client.get("/api/health", headers={"X-Request-ID": "test-rid-123"})
    assert response.headers["X-Request-ID"] == "test-rid-123"


def test_root_serves_spa_or_placeholder(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    content_type = response.headers["content-type"]
    # Built SPA present -> index.html; otherwise a JSON placeholder pointing at the API.
    assert "text/html" in content_type or "application/json" in content_type


def test_openapi_schema_is_exposed(client: TestClient) -> None:
    response = client.get("/api/openapi.json")
    assert response.status_code == 200
    assert "/api/health" in response.json()["paths"]


def test_unknown_api_route_is_json_404_not_spa(client: TestClient) -> None:
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert "application/json" in response.headers["content-type"]


def test_missing_asset_is_404_not_index_html(client: TestClient) -> None:
    from app.core.config import get_settings
    from app.main import find_static_dir

    if find_static_dir(get_settings()) is None:
        pytest.skip("frontend build not present")
    response = client.get("/assets/index-DEADBEEF.js")
    assert response.status_code == 404


def test_deep_route_still_gets_spa_fallback(client: TestClient) -> None:
    from app.core.config import get_settings
    from app.main import find_static_dir

    if find_static_dir(get_settings()) is None:
        pytest.skip("frontend build not present")
    response = client.get("/tracker/settled")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_percent_in_database_url_survives_alembic_config() -> None:
    from app.db.migrate import build_alembic_config

    url = "postgresql+asyncpg://user:p%40ss@localhost/db"
    cfg = build_alembic_config(url)
    # configparser interpolation must not corrupt or reject the URL
    assert cfg.get_main_option("sqlalchemy.url") == url


def test_malformed_redis_url_reports_down_not_500(client: TestClient) -> None:
    import os

    from app.core.config import get_settings

    os.environ["REDIS_URL"] = "localhost:6379"  # missing scheme
    get_settings.cache_clear()
    try:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["redis"]["status"] == "down"
        assert body["status"] == "degraded"
    finally:
        os.environ["REDIS_URL"] = ""
        get_settings.cache_clear()
