"""Shared test fixtures.

DATABASE_URL is pointed at a per-session temporary SQLite file before the app
is imported; the real Alembic migrations then build the schema (so tests catch
model/migration drift) and the YAML seed runs once. NullPool keeps connections
from being reused across event loops (TestClient and pytest-asyncio each run
their own).
"""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

_TMP_DIR = Path(tempfile.mkdtemp(prefix="gamestakes-test-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DIR}/test.db"
os.environ["DATABASE_POOL"] = "null"
os.environ["REDIS_URL"] = ""
os.environ["ENVIRONMENT"] = "test"
# Tests drive ingestion explicitly; no background jobs.
os.environ["SCHEDULER_ENABLED"] = "false"


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    """Migrate to head and seed the catalogue once per test session."""
    from alembic import command

    from app.db.migrate import build_alembic_config
    from app.db.session import get_sessionmaker
    from app.ingest.seed import seed_catalog

    command.upgrade(build_alembic_config(), "head")

    async def _seed() -> None:
        async with get_sessionmaker()() as session:
            await seed_catalog(session)

    asyncio.run(_seed())


@pytest.fixture(scope="session")
def client(_database: None) -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def db_session(_database: None) -> AsyncIterator[AsyncSession]:
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        yield session
