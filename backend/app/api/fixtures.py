"""Schedule endpoints: fixtures by date range, sport, league or team."""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import SessionDep
from app.db.enums import FixtureStatus, Side
from app.db.models import (
    Fixture,
    FixtureParticipant,
    League,
    Player,
    Result,
    Sport,
    Team,
    Venue,
)

router = APIRouter(tags=["fixtures"])

MAX_RANGE_DAYS = 90


class ParticipantOut(BaseModel):
    side: Side
    name: str
    team_id: int | None = None
    player_id: int | None = None
    logo_url: str | None = None
    score: int | None = None


class FixtureOut(BaseModel):
    id: int
    sport_slug: str
    league_slug: str
    league_name: str
    start_time: datetime
    status: FixtureStatus
    round: str | None
    event_name: str | None
    venue: str | None
    home: ParticipantOut
    away: ParticipantOut
    has_prediction: bool = False


class FixturePage(BaseModel):
    fixtures: list[FixtureOut]
    total: int
    start: date
    end: date


@router.get("/fixtures")
async def list_fixtures(
    session: SessionDep,
    start: date | None = None,
    end: date | None = None,
    sport: str | None = None,
    league: str | None = None,
    team_id: int | None = None,
    status: FixtureStatus | None = None,
    limit: int = Query(default=200, le=500),
    offset: int = 0,
) -> FixturePage:
    """Fixtures in a date range. Defaults to today through the next week."""
    start = start or date.today()
    end = end or start + timedelta(days=7)
    if end < start:
        raise HTTPException(status_code=422, detail="`end` must not precede `start`")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"range must be {MAX_RANGE_DAYS} days or fewer")

    stmt = (
        select(Fixture, League, Sport, Venue, Result)
        .join(League, Fixture.league_id == League.id)
        .join(Sport, Fixture.sport_id == Sport.id)
        .outerjoin(Venue, Fixture.venue_id == Venue.id)
        .outerjoin(Result, Result.fixture_id == Fixture.id)
        .options(selectinload(Fixture.participants))
        .where(
            Fixture.start_time >= _day_start(start),
            Fixture.start_time < _day_start(end + timedelta(days=1)),
        )
        .order_by(Fixture.start_time, Fixture.id)
    )
    if sport:
        stmt = stmt.where(Sport.slug == sport)
    if league:
        stmt = stmt.where(League.slug == league)
    if status:
        stmt = stmt.where(Fixture.status == status)
    if team_id:
        stmt = stmt.where(
            Fixture.id.in_(
                select(FixtureParticipant.fixture_id).where(FixtureParticipant.team_id == team_id)
            )
        )

    rows = (await session.execute(stmt.limit(limit).offset(offset))).all()
    fixtures = [await _to_out(session, *row) for row in rows]
    return FixturePage(fixtures=fixtures, total=len(fixtures), start=start, end=end)


@router.get("/fixtures/{fixture_id}")
async def get_fixture(fixture_id: int, session: SessionDep) -> FixtureOut:
    stmt = (
        select(Fixture, League, Sport, Venue, Result)
        .join(League, Fixture.league_id == League.id)
        .join(Sport, Fixture.sport_id == Sport.id)
        .outerjoin(Venue, Fixture.venue_id == Venue.id)
        .outerjoin(Result, Result.fixture_id == Fixture.id)
        .options(selectinload(Fixture.participants))
        .where(Fixture.id == fixture_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"fixture {fixture_id} not found")
    return await _to_out(session, *row)


def _day_start(day: date) -> datetime:
    from datetime import UTC

    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


async def _to_out(
    session: SessionDep,
    fixture: Fixture,
    league: League,
    sport: Sport,
    venue: Venue | None,
    result: Result | None,
) -> FixtureOut:
    sides: dict[Side, ParticipantOut] = {}
    for participant in fixture.participants:
        name, logo = "", None
        if participant.team_id is not None:
            team = await session.get(Team, participant.team_id)
            name, logo = (team.name, team.logo_url) if team else ("", None)
        elif participant.player_id is not None:
            player = await session.get(Player, participant.player_id)
            name = player.name if player else ""
        score = None
        if result is not None:
            score = result.home_score if participant.side == Side.HOME else result.away_score
        sides[participant.side] = ParticipantOut(
            side=participant.side,
            name=name,
            team_id=participant.team_id,
            player_id=participant.player_id,
            logo_url=logo,
            score=score,
        )

    blank = ParticipantOut(side=Side.HOME, name="TBD")
    return FixtureOut(
        id=fixture.id,
        sport_slug=sport.slug,
        league_slug=league.slug,
        league_name=league.name,
        start_time=fixture.start_time,
        status=fixture.status,
        round=fixture.round,
        event_name=fixture.event_name,
        venue=venue.name if venue else None,
        home=sides.get(Side.HOME, blank),
        away=sides.get(Side.AWAY, blank.model_copy(update={"side": Side.AWAY})),
    )
