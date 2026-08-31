"""Health endpoint reporting database, Redis and provider status.

Always returns 200 with a status field — the container HEALTHCHECK verifies
reachability; a degraded dependency is reported, not hidden behind a 5xx that
would make Docker restart a perfectly reachable app.
"""

from typing import Literal

import redis.asyncio as aioredis
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app import __version__
from app.core.config import get_settings
from app.db.session import get_engine

router = APIRouter(tags=["health"])


class ComponentStatus(BaseModel):
    status: Literal["up", "down", "disabled"]
    detail: str | None = None


class ProviderStatus(BaseModel):
    name: str
    status: Literal["up", "down", "disabled", "degraded"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    version: str
    database: ComponentStatus
    redis: ComponentStatus
    providers: list[ProviderStatus]


async def _check_database() -> ComponentStatus:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return ComponentStatus(status="up")
    except Exception as exc:  # noqa: BLE001 — report, never crash the health endpoint
        return ComponentStatus(status="down", detail=str(exc))


async def _check_redis() -> ComponentStatus:
    settings = get_settings()
    if not settings.redis_url:
        return ComponentStatus(status="disabled", detail="REDIS_URL not configured")
    client: aioredis.Redis | None = None
    try:
        # from_url raises synchronously on a malformed URL — keep it guarded too.
        client = aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        await client.ping()
        return ComponentStatus(status="up")
    except Exception as exc:  # noqa: BLE001
        return ComponentStatus(status="down", detail=str(exc))
    finally:
        if client is not None:
            await client.aclose()


@router.get("/health")
async def health() -> HealthResponse:
    settings = get_settings()
    database = await _check_database()
    redis_status = await _check_redis()

    degraded = database.status == "down" or redis_status.status == "down"
    return HealthResponse(
        status="degraded" if degraded else "ok",
        app=settings.app_name,
        version=__version__,
        database=database,
        redis=redis_status,
        providers=[],  # populated in Phase 3 when provider adapters exist
    )
