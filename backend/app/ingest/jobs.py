"""Ingestion job bookkeeping.

Every job records start, finish, row counts and any error to `ingest_jobs`, so
a failed or thin run is visible in the UI instead of looking like "no data
today". The context manager guarantees a terminal row even when the body
raises.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import JobStatus
from app.db.models import IngestJob

log = get_logger(__name__)


@dataclass
class JobRecorder:
    """Handle passed to job bodies for reporting counts and detail."""

    job: IngestJob
    rows_fetched: int = 0
    rows_upserted: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def count(self, fetched: int = 0, upserted: int = 0) -> None:
        self.rows_fetched += fetched
        self.rows_upserted += upserted

    def note(self, **values: Any) -> None:
        self.detail.update(values)


@asynccontextmanager
async def track_job(
    session: AsyncSession, job_name: str, provider: str | None = None
) -> AsyncIterator[JobRecorder]:
    job = IngestJob(
        job_name=job_name,
        provider=provider,
        status=JobStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    session.add(job)
    await session.commit()

    recorder = JobRecorder(job=job)
    try:
        yield recorder
    except Exception as exc:
        job.status = JobStatus.FAILURE
        job.error = f"{type(exc).__name__}: {exc}"
        job.finished_at = datetime.now(UTC)
        job.rows_fetched = recorder.rows_fetched
        job.rows_upserted = recorder.rows_upserted
        job.detail = recorder.detail
        await session.commit()
        log.error("job.failed", job=job_name, provider=provider, error=str(exc))
        raise
    else:
        job.status = JobStatus.SUCCESS
        job.finished_at = datetime.now(UTC)
        job.rows_fetched = recorder.rows_fetched
        job.rows_upserted = recorder.rows_upserted
        job.detail = recorder.detail
        await session.commit()
        # detail is nested, not splatted: job bodies record their own keys
        # (including "provider") and would otherwise collide with these.
        log.info(
            "job.complete",
            job=job_name,
            provider=provider,
            fetched=recorder.rows_fetched,
            upserted=recorder.rows_upserted,
            detail=recorder.detail,
        )
