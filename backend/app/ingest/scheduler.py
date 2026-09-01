"""APScheduler wiring for recurring ingestion.

Cadence is deliberately frugal. Free tiers are the binding constraint — The
Odds API bills 500 credits a month — so schedules are conservative by default
and every interval is configurable. Each run writes an `ingest_jobs` row, so a
skipped or failing schedule is visible rather than silently absent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_sessionmaker

log = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def ingest_upcoming_fixtures(days_ahead: int = 7) -> None:
    """Refresh the forward slate for every enabled league."""
    from sqlalchemy import select

    from app.db.models import League
    from app.ingest.fixtures import FixtureIngestor

    today = datetime.now(UTC).date()
    async with get_sessionmaker()() as session:
        leagues = (
            (await session.execute(select(League).where(League.is_active.is_(True))))
            .scalars()
            .all()
        )
        ingestor = FixtureIngestor(session)
        for league in leagues:
            try:
                await ingestor.ingest_league(league.slug, today, today + timedelta(days=days_ahead))
            except Exception:  # noqa: BLE001 — one league must not stop the rest
                log.exception("ingest.league_failed", league=league.slug)


async def ingest_recent_results(days_back: int = 3) -> None:
    """Pull results for recently played fixtures so settlement has data."""
    from sqlalchemy import select

    from app.db.models import League
    from app.ingest.fixtures import FixtureIngestor

    today = datetime.now(UTC).date()
    async with get_sessionmaker()() as session:
        leagues = (
            (await session.execute(select(League).where(League.is_active.is_(True))))
            .scalars()
            .all()
        )
        ingestor = FixtureIngestor(session)
        for league in leagues:
            try:
                await ingestor.ingest_results(league.slug, today - timedelta(days=days_back), today)
            except Exception:  # noqa: BLE001
                log.exception("ingest.results_failed", league=league.slug)


async def refresh_odds() -> None:
    """Snapshot prices for fixtures kicking off soon."""
    from app.ingest.odds import ingest_odds_for_upcoming

    async with get_sessionmaker()() as session:
        await ingest_odds_for_upcoming(session)


async def refresh_predictions() -> None:
    """Refit models and regenerate predictions for upcoming fixtures."""
    from app.models.pipeline import predict_all_leagues

    async with get_sessionmaker()() as session:
        await predict_all_leagues(session, sport="football")


async def settle_finished() -> None:
    """Grade predictions and bets whose fixtures have finished."""
    from app.scoring.settlement import Settler

    async with get_sessionmaker()() as session:
        await Settler(session).settle_all()


def build_scheduler() -> AsyncIOScheduler:
    """Schedule the recurring jobs, each with a first run shortly after startup.

    An IntervalTrigger without a start_date fires its *first* run one whole
    interval after the scheduler starts — so a fresh container would sit idle
    for six hours before fetching a single fixture, which reads as "I added my
    keys and nothing happened". Each job therefore gets an explicit first run a
    few minutes out.

    The offsets stagger in dependency order — fixtures before the odds that
    attach to them, results before the predictions that train on them — and
    spread the load so five jobs do not hit rate-limited providers at once.

    Note that a restart re-triggers this initial round. That is cheap for
    fixtures and results, and deliberately later for odds, which cost credits.
    """
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    def first_run(minutes: float) -> datetime:
        return datetime.now(UTC) + timedelta(minutes=minutes)

    scheduler.add_job(
        ingest_upcoming_fixtures,
        IntervalTrigger(hours=settings.fixtures_refresh_hours, start_date=first_run(1)),
        id="fixtures",
        name="Refresh upcoming fixtures",
        max_instances=1,
        coalesce=True,  # a missed run is skipped, never queued up
    )
    scheduler.add_job(
        ingest_recent_results,
        IntervalTrigger(hours=settings.results_refresh_hours, start_date=first_run(4)),
        id="results",
        name="Refresh recent results",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        refresh_odds,
        IntervalTrigger(hours=settings.odds_refresh_hours, start_date=first_run(8)),
        id="odds",
        name="Snapshot odds",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        refresh_predictions,
        IntervalTrigger(hours=settings.predictions_refresh_hours, start_date=first_run(12)),
        id="predictions",
        name="Refresh predictions",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        settle_finished,
        IntervalTrigger(hours=settings.results_refresh_hours, start_date=first_run(15)),
        id="settle",
        name="Settle finished fixtures",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        log.info("scheduler.disabled", detail="SCHEDULER_ENABLED=false")
        return None
    if _scheduler is None:
        _scheduler = build_scheduler()
        _scheduler.start()
        log.info(
            "scheduler.started",
            jobs=[j.id for j in _scheduler.get_jobs()],
            timezone=settings.timezone,
        )
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def describe_jobs() -> list[dict[str, object]]:
    """Job list for the admin UI: what runs, and when it next fires."""
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        }
        for job in _scheduler.get_jobs()
    ]


async def run_job_now(job_id: str) -> None:
    """Trigger a scheduled job immediately (admin action)."""
    jobs = {
        "fixtures": ingest_upcoming_fixtures,
        "results": ingest_recent_results,
        "odds": refresh_odds,
        "predictions": refresh_predictions,
        "settle": settle_finished,
    }
    handler = jobs.get(job_id)
    if handler is None:
        raise ValueError(f"unknown job '{job_id}'")
    await handler()


def today_utc() -> date:
    return datetime.now(UTC).date()
