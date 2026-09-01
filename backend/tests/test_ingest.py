"""End-to-end ingestion: provider payload -> resolved entities -> canonical rows."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ProviderError
from app.db.enums import FixtureStatus, JobStatus, Side
from app.db.models import (
    Fixture,
    FixtureParticipant,
    IngestJob,
    RawIngest,
    Result,
    Team,
)
from app.ingest.fixtures import FixtureIngestor
from app.providers.base import (
    BaseProvider,
    ProviderCapability,
    ProviderHealth,
    ProviderState,
    RawFixture,
    RawParticipant,
    RawResult,
)
from app.providers.registry import ProviderRegistry

START, END = date(2026, 9, 1), date(2026, 9, 30)
KICKOFF = datetime(2026, 9, 12, 14, 0, tzinfo=UTC)


class FakeProvider(BaseProvider):
    def __init__(
        self,
        name: str = "fake",
        fixtures: list[RawFixture] | None = None,
        results: list[RawResult] | None = None,
        fail: bool = False,
    ):
        self.name = name
        self.supported_leagues = {"premier-league"}
        self.capabilities = {ProviderCapability.FIXTURES, ProviderCapability.RESULTS}
        self._fixtures = fixtures or []
        self._results = results or []
        self._fail = fail

    async def fetch_fixtures(self, league: str, start: date, end: date) -> list[RawFixture]:
        if self._fail:
            raise ProviderError("upstream exploded")
        return self._fixtures

    async def fetch_results(self, league: str, start: date, end: date) -> list[RawResult]:
        if self._fail:
            raise ProviderError("upstream exploded")
        return self._results

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, state=ProviderState.UP)


def _fixture(external_id: str, home: str, away: str) -> RawFixture:
    return RawFixture(
        external_id=external_id,
        league_code="premier-league",
        start_time=KICKOFF,
        status="scheduled",
        home=RawParticipant(name=home, external_id=f"t-{home}", is_home=True),
        away=RawParticipant(name=away, external_id=f"t-{away}", is_home=False),
        round="4",
    )


def _ingestor(session: AsyncSession, provider: BaseProvider) -> FixtureIngestor:
    return FixtureIngestor(session, registry=ProviderRegistry([provider]))


async def test_first_ingest_creates_teams_and_fixture(db_session: AsyncSession) -> None:
    provider = FakeProvider(fixtures=[_fixture("m1", "Ingest United", "Ingest City")])

    counts = await _ingestor(db_session, provider).ingest_league("premier-league", START, END)

    assert counts == {"fetched": 1, "upserted": 1, "unresolved": 0}
    fixture = (
        (await db_session.execute(select(Fixture).where(Fixture.start_time == KICKOFF)))
        .scalars()
        .first()
    )
    assert fixture is not None
    assert fixture.status == FixtureStatus.SCHEDULED
    assert fixture.round == "4"

    participants = (
        (
            await db_session.execute(
                select(FixtureParticipant).where(FixtureParticipant.fixture_id == fixture.id)
            )
        )
        .scalars()
        .all()
    )
    assert {p.side for p in participants} == {Side.HOME, Side.AWAY}
    assert all(p.team_id is not None for p in participants)


async def test_reingest_is_idempotent(db_session: AsyncSession) -> None:
    provider = FakeProvider(fixtures=[_fixture("m2", "Repeat Rovers", "Repeat Athletic")])
    ingestor = _ingestor(db_session, provider)

    await ingestor.ingest_league("premier-league", START, END)
    before = await db_session.scalar(select(func.count(Fixture.id)))
    await ingestor.ingest_league("premier-league", START, END)
    after = await db_session.scalar(select(func.count(Fixture.id)))

    assert before == after, "the same provider fixture must not create a second row"


async def test_raw_payload_is_persisted_before_normalisation(
    db_session: AsyncSession,
) -> None:
    provider = FakeProvider(fixtures=[_fixture("m3", "Raw Town", "Raw County")])

    await _ingestor(db_session, provider).ingest_league("premier-league", START, END)

    # Other tests write to the same endpoint, so take this run's own row.
    raw = (
        (
            await db_session.execute(
                select(RawIngest)
                .where(RawIngest.endpoint == "fixtures/premier-league")
                .order_by(RawIngest.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .one()
    )
    assert raw.content_hash
    assert any(item["external_id"] == "m3" for item in raw.payload)


async def test_results_attach_scores_and_winner(db_session: AsyncSession) -> None:
    fixture_payload = _fixture("m4", "Score Rangers", "Score Wanderers")
    provider = FakeProvider(
        fixtures=[fixture_payload],
        results=[
            RawResult(
                external_id="m4",
                league_code="premier-league",
                status="finished",
                home_score=3,
                away_score=1,
                detail={"half_time": {"home": 2, "away": 0}},
            )
        ],
    )
    ingestor = _ingestor(db_session, provider)
    await ingestor.ingest_league("premier-league", START, END)

    counts = await ingestor.ingest_results("premier-league", START, END)

    assert counts["upserted"] == 1
    result = (
        await db_session.execute(select(Result).order_by(Result.id.desc()).limit(1))
    ).scalar_one()
    assert (result.home_score, result.away_score) == (3, 1)
    assert result.winner_side == Side.HOME
    assert result.finalized_at is not None
    fixture = await db_session.get(Fixture, result.fixture_id)
    assert fixture is not None and fixture.status == FixtureStatus.FINISHED


async def test_draw_records_no_winner(db_session: AsyncSession) -> None:
    provider = FakeProvider(
        fixtures=[_fixture("m5", "Draw Albion", "Draw Athletic")],
        results=[
            RawResult(
                external_id="m5",
                league_code="premier-league",
                status="finished",
                home_score=2,
                away_score=2,
            )
        ],
    )
    ingestor = _ingestor(db_session, provider)
    await ingestor.ingest_league("premier-league", START, END)
    await ingestor.ingest_results("premier-league", START, END)

    result = (
        await db_session.execute(select(Result).order_by(Result.id.desc()).limit(1))
    ).scalar_one()
    assert result.winner_side is None


async def test_job_row_records_success_and_counts(db_session: AsyncSession) -> None:
    provider = FakeProvider(fixtures=[_fixture("m6", "Job Town", "Job City")])

    await _ingestor(db_session, provider).ingest_league("premier-league", START, END)

    job = (
        await db_session.execute(
            select(IngestJob)
            .where(IngestJob.job_name == "fixtures:premier-league")
            .order_by(IngestJob.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert job.status == JobStatus.SUCCESS
    assert job.rows_fetched == 1
    assert job.rows_upserted == 1
    assert job.finished_at is not None
    assert job.detail["provider"] == "fake"


async def test_provider_failure_is_recorded_not_swallowed(
    db_session: AsyncSession,
) -> None:
    """Every provider failing is a real outcome: no rows, and a job row saying so."""
    provider = FakeProvider(fail=True)

    counts = await _ingestor(db_session, provider).ingest_league("premier-league", START, END)

    assert counts["fetched"] == 0
    job = (
        await db_session.execute(
            select(IngestJob)
            .where(IngestJob.job_name == "fixtures:premier-league")
            .order_by(IngestJob.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert job.status == JobStatus.SUCCESS  # the job ran; the providers did not
    assert job.detail["degraded"] is True
    assert job.detail["attempts"][0]["ok"] is False


async def test_unresolvable_team_is_skipped_not_guessed(
    db_session: AsyncSession,
) -> None:
    """Once a sport has teams, an unknown ambiguous name must queue, not invent."""
    football_teams = [
        Team(sport_id=1, name=n, normalized_name=n.casefold())
        for n in ("Skiptest Rovers A", "Skiptest Rovers B")
    ]
    db_session.add_all(football_teams)
    await db_session.flush()

    provider = FakeProvider(fixtures=[_fixture("m7", "Skiptest Rovers C", "Skiptest Rovers D")])
    counts = await _ingestor(db_session, provider).ingest_league("premier-league", START, END)

    assert counts["upserted"] == 0
    assert counts["unresolved"] == 1


async def test_unknown_league_raises_clearly(db_session: AsyncSession) -> None:
    provider = FakeProvider()
    with pytest.raises(ValueError, match="unknown league"):
        await _ingestor(db_session, provider).ingest_league("not-a-league", START, END)


async def test_auto_created_teams_do_not_leave_the_queue_full(
    db_session: AsyncSession,
) -> None:
    """Resolution opens a queue item before the ingestor decides what to do.
    When it goes on to create the team itself, that item describes a question
    already answered — leaving it would bury real ambiguity under one row per
    team ever seen.
    """
    from app.db.enums import ResolutionStatus
    from app.db.models import ResolutionQueueItem

    before = len(
        (
            await db_session.execute(
                select(ResolutionQueueItem).where(
                    ResolutionQueueItem.status == ResolutionStatus.PENDING
                )
            )
        )
        .scalars()
        .all()
    )

    provider = FakeProvider(fixtures=[_fixture("q1", "Queueclean Athletic", "Queueclean Rangers")])
    counts = await _ingestor(db_session, provider).ingest_league("premier-league", START, END)
    assert counts["upserted"] == 1, "both teams should have been created"

    after = (
        (
            await db_session.execute(
                select(ResolutionQueueItem).where(
                    ResolutionQueueItem.status == ResolutionStatus.PENDING
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(after) == before, "auto-created teams must not leave pending items"

    resolved = (
        (
            await db_session.execute(
                select(ResolutionQueueItem).where(
                    ResolutionQueueItem.raw_name == "Queueclean Athletic"
                )
            )
        )
        .scalars()
        .all()
    )
    assert resolved and resolved[0].status == ResolutionStatus.RESOLVED
    assert resolved[0].resolved_entity_id is not None
