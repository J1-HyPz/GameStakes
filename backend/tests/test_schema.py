"""Schema semantics: participants for team and combat sports, constraints."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import FixtureStatus, JobStatus, Side, VictoryMethod
from app.db.models import (
    BoutResult,
    Fixture,
    FixtureParticipant,
    IngestJob,
    League,
    OddsSnapshot,
    Player,
    Result,
    Sport,
    Team,
)

KICKOFF = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)


async def _sport(session: AsyncSession, slug: str) -> Sport:
    return (await session.execute(select(Sport).where(Sport.slug == slug))).scalar_one()


async def _league(session: AsyncSession, slug: str) -> League:
    return (await session.execute(select(League).where(League.slug == slug))).scalar_one()


async def test_team_fixture_roundtrip(db_session: AsyncSession) -> None:
    football = await _sport(db_session, "football")
    epl = await _league(db_session, "premier-league")
    home = Team(sport_id=football.id, name="Schema Test Home", normalized_name="schema test home")
    away = Team(sport_id=football.id, name="Schema Test Away", normalized_name="schema test away")
    db_session.add_all([home, away])
    await db_session.flush()

    fixture = Fixture(
        sport_id=football.id,
        league_id=epl.id,
        start_time=KICKOFF,
        status=FixtureStatus.FINISHED,
    )
    db_session.add(fixture)
    await db_session.flush()
    db_session.add_all(
        [
            FixtureParticipant(fixture_id=fixture.id, side=Side.HOME, team_id=home.id),
            FixtureParticipant(fixture_id=fixture.id, side=Side.AWAY, team_id=away.id),
        ]
    )
    db_session.add(
        Result(
            fixture_id=fixture.id,
            home_score=2,
            away_score=1,
            winner_side=Side.HOME,
            score_detail={"ht": [1, 0]},
        )
    )
    db_session.add(
        OddsSnapshot(
            fixture_id=fixture.id,
            bookmaker="bet365",
            market="1x2",
            selection="home",
            price_decimal=Decimal("1.910"),
            provider="the-odds-api",
            captured_at=KICKOFF,
        )
    )
    await db_session.commit()

    loaded = await db_session.get(Fixture, fixture.id)
    assert loaded is not None


async def test_combat_bout_is_first_class(db_session: AsyncSession) -> None:
    mma = await _sport(db_session, "mma")
    ufc = await _league(db_session, "ufc")
    red = Player(sport_id=mma.id, name="Test Fighter Red", normalized_name="test fighter red")
    blue = Player(sport_id=mma.id, name="Test Fighter Blue", normalized_name="test fighter blue")
    db_session.add_all([red, blue])
    await db_session.flush()

    bout = Fixture(
        sport_id=mma.id,
        league_id=ufc.id,
        start_time=KICKOFF,
        status=FixtureStatus.FINISHED,
        event_name="UFC 999",
        scheduled_rounds=5,
    )
    db_session.add(bout)
    await db_session.flush()
    db_session.add_all(
        [
            FixtureParticipant(fixture_id=bout.id, side=Side.HOME, player_id=red.id),
            FixtureParticipant(fixture_id=bout.id, side=Side.AWAY, player_id=blue.id),
        ]
    )
    result = Result(fixture_id=bout.id, winner_side=Side.HOME)
    db_session.add(result)
    await db_session.flush()
    db_session.add(
        BoutResult(
            result_id=result.id,
            method=VictoryMethod.SUBMISSION,
            end_round=2,
            end_time_seconds=143,
            detail="rear-naked choke",
        )
    )
    await db_session.commit()


async def test_participant_must_be_team_xor_player(db_session: AsyncSession) -> None:
    football = await _sport(db_session, "football")
    epl = await _league(db_session, "premier-league")
    team = Team(sport_id=football.id, name="XOR Team", normalized_name="xor team")
    player = Player(sport_id=football.id, name="XOR Player", normalized_name="xor player")
    db_session.add_all([team, player])
    await db_session.flush()
    fixture = Fixture(sport_id=football.id, league_id=epl.id, start_time=KICKOFF)
    db_session.add(fixture)
    await db_session.flush()

    db_session.add(
        FixtureParticipant(
            fixture_id=fixture.id, side=Side.HOME, team_id=team.id, player_id=player.id
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_one_participant_per_side(db_session: AsyncSession) -> None:
    football = await _sport(db_session, "football")
    epl = await _league(db_session, "premier-league")
    a = Team(sport_id=football.id, name="Side Dup A", normalized_name="side dup a")
    b = Team(sport_id=football.id, name="Side Dup B", normalized_name="side dup b")
    db_session.add_all([a, b])
    await db_session.flush()
    fixture = Fixture(sport_id=football.id, league_id=epl.id, start_time=KICKOFF)
    db_session.add(fixture)
    await db_session.flush()

    db_session.add_all(
        [
            FixtureParticipant(fixture_id=fixture.id, side=Side.HOME, team_id=a.id),
            FixtureParticipant(fixture_id=fixture.id, side=Side.HOME, team_id=b.id),
        ]
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_timestamps_round_trip_as_aware_utc(db_session: AsyncSession) -> None:
    """SQLite has no tz-aware storage: without TZDateTime an aware non-UTC
    input would be stored at the wrong instant and read back naive."""
    bst = timezone(timedelta(hours=1))
    job = IngestJob(
        job_name="tz-roundtrip",
        status=JobStatus.SUCCESS,
        started_at=datetime(2026, 9, 12, 16, 0, tzinfo=bst),
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    assert job.started_at == datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    assert job.started_at.tzinfo is not None
    assert job.started_at < datetime.now(UTC) or job.started_at > datetime.now(UTC)


async def test_naive_timestamp_is_rejected(db_session: AsyncSession) -> None:
    db_session.add(
        IngestJob(
            job_name="tz-naive",
            status=JobStatus.SUCCESS,
            started_at=datetime(2026, 9, 12, 15, 0),  # noqa: DTZ001 — the point of the test
        )
    )
    with pytest.raises(StatementError):
        await db_session.flush()
    await db_session.rollback()


async def test_enum_columns_reject_invalid_values_at_the_database(
    db_session: AsyncSession,
) -> None:
    """Guards write paths that bypass the ORM (bulk loads, raw SQL fixes)."""
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO ingest_jobs (job_name, status, started_at, "
                "rows_fetched, rows_upserted, detail) "
                "VALUES ('bad', 'not-a-status', CURRENT_TIMESTAMP, 0, 0, '{}')"
            )
        )
    await db_session.rollback()
