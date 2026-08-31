"""Ingestion job admin: schedule, history, and manual triggers."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import SessionDep
from app.db.enums import JobStatus
from app.db.models import IngestJob
from app.ingest.scheduler import describe_jobs, run_job_now

router = APIRouter(prefix="/jobs", tags=["jobs"])


class ScheduledJob(BaseModel):
    id: str
    name: str
    next_run: str | None
    trigger: str


class JobRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_name: str
    provider: str | None
    status: JobStatus
    started_at: datetime
    finished_at: datetime | None
    rows_fetched: int
    rows_upserted: int
    error: str | None
    detail: dict[str, Any]


@router.get("/schedule")
async def scheduled_jobs() -> list[ScheduledJob]:
    return [ScheduledJob.model_validate(job) for job in describe_jobs()]


@router.get("/runs")
async def job_runs(
    session: SessionDep,
    status: JobStatus | None = None,
    limit: int = 50,
) -> list[JobRun]:
    """Recent runs, newest first — the 'no silent failures' view."""
    stmt = select(IngestJob).order_by(IngestJob.started_at.desc()).limit(min(limit, 200))
    if status:
        stmt = stmt.where(IngestJob.status == status)
    runs = (await session.execute(stmt)).scalars().all()
    return [JobRun.model_validate(run) for run in runs]


@router.post("/{job_id}/run")
async def trigger_job(job_id: str) -> dict[str, str]:
    try:
        await run_job_now(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "completed", "job": job_id}
