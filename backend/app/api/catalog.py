"""Sports and leagues catalogue endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.api.deps import SessionDep
from app.db.enums import CompetitionType, SportKind
from app.db.models import League, Sport

router = APIRouter(tags=["catalog"])


class SportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    kind: SportKind
    league_count: int


class LeagueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    short_name: str | None
    country: str | None
    competition_type: CompetitionType
    tier: int | None
    is_active: bool
    sport_slug: str


@router.get("/sports")
async def list_sports(session: SessionDep) -> list[SportOut]:
    stmt = (
        select(Sport, func.count(League.id))
        .join(League, (League.sport_id == Sport.id) & League.is_active.is_(True), isouter=True)
        .group_by(Sport.id)
        .order_by(Sport.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        SportOut(id=s.id, slug=s.slug, name=s.name, kind=s.kind, league_count=count)
        for s, count in rows
    ]


@router.get("/leagues")
async def list_leagues(
    session: SessionDep,
    sport: str | None = None,
    include_inactive: bool = False,
) -> list[LeagueOut]:
    stmt = select(League, Sport.slug).join(Sport, League.sport_id == Sport.id).order_by(League.id)
    if sport is not None:
        stmt = stmt.where(Sport.slug == sport)
    if not include_inactive:
        stmt = stmt.where(League.is_active.is_(True))
    rows = (await session.execute(stmt)).all()
    return [
        LeagueOut(
            id=le.id,
            slug=le.slug,
            name=le.name,
            short_name=le.short_name,
            country=le.country,
            competition_type=le.competition_type,
            tier=le.tier,
            is_active=le.is_active,
            sport_slug=sport_slug,
        )
        for le, sport_slug in rows
    ]


@router.get("/leagues/{slug}")
async def get_league(slug: str, session: SessionDep) -> LeagueOut:
    stmt = (
        select(League, Sport.slug)
        .join(Sport, League.sport_id == Sport.id)
        .where(League.slug == slug)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"league '{slug}' not found")
    le, sport_slug = row
    return LeagueOut(
        id=le.id,
        slug=le.slug,
        name=le.name,
        short_name=le.short_name,
        country=le.country,
        competition_type=le.competition_type,
        tier=le.tier,
        is_active=le.is_active,
        sport_slug=sport_slug,
    )
