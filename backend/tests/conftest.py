"""Shared test fixtures.

DATABASE_URL is pointed at a per-session temporary SQLite file before the app
is imported, so tests never touch a real database.
"""

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP_DIR = Path(tempfile.mkdtemp(prefix="gamestakes-test-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DIR}/test.db"
os.environ["REDIS_URL"] = ""
os.environ["ENVIRONMENT"] = "test"


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
