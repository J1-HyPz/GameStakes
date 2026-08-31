"""Bet builder and tracker endpoints."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import SessionDep
from app.betting.candidates import build_candidates
from app.betting.parlay import Leg
from app.betting.staking import drawdown_warning, loss_frequency_phrase
from app.betting.tiers import DEFAULT_TIERS, TierResult, build_all_tiers
from app.core.config import get_settings
from app.db.enums import BetStatus, BetTier, Confidence
from app.db.models import Bet, BetLeg, Fixture, League
from app.scoring.ledger import exposure_summary

router = APIRouter(prefix="/bets", tags=["bets"])


class LegOut(BaseModel):
    fixture_id: int
    fixture: str
    league: str
    kick_off: datetime
    market: str
    selection: str
    line: float | None
    price_decimal: float
    bookmaker: str | None
    model_probability: float
    implied_probability: float | None
    edge: float | None
    confidence: str
    reasoning: str


class TierOut(BaseModel):
    """A tier's bet, or an explanation of why there isn't one."""

    tier: BetTier
    has_bet: bool
    legs: list[LegOut] = []
    combined_odds: float | None = None
    combined_probability: float | None = None
    naive_probability: float | None = None
    correlation_effect: float | None = None
    expected_value: float | None = None
    edge: float | None = None
    stake: float | None = None
    stake_fraction: float | None = None
    kelly_fraction: float | None = None
    capped_by: str | None = None
    projected_return: float | None = None
    loss_frequency: str | None = None
    candidates_considered: int = 0
    candidates_qualifying: int = 0
    reason: str | None = None
    copy_text: str | None = None


class BuilderResponse(BaseModel):
    tiers: list[TierOut]
    bankroll: float
    currency: str
    staking_note: str
    slate_start: datetime
    slate_end: datetime
    disclaimer: str = (
        "Model probabilities are estimates, not certainties. Negative runs are expected "
        "even with a genuine edge."
    )


class TrackedBetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tier: BetTier
    status: BetStatus
    stake: Decimal
    currency: str
    combined_price_decimal: Decimal
    combined_probability: float
    expected_value: float | None
    placed_at: datetime | None
    settled_at: datetime | None
    payout: Decimal | None


class TrackBetIn(BaseModel):
    tier: BetTier
    stake: float
    combined_price_decimal: float
    combined_probability: float
    naive_probability: float | None = None
    expected_value: float | None = None
    kelly_fraction: float | None = None
    legs: list[dict[str, Any]]


@router.get("/builder")
async def builder(
    session: SessionDep,
    hours_ahead: int = Query(default=48, le=336),
    sport: str | None = None,
    league: str | None = None,
    min_confidence: Confidence = Confidence.MEDIUM,
) -> BuilderResponse:
    """Generate one bet per risk tier for the slate.

    A tier with nothing qualifying returns no bet and says why. That is the
    correct output, not a failure — filling the slot with a marginal bet is how
    a tool teaches its user to lose money.
    """
    settings = get_settings()
    bankroll = Decimal(str(settings.bankroll))
    start = datetime.now(UTC)
    end = start + timedelta(hours=hours_ahead)

    if bankroll <= 0:
        return BuilderResponse(
            tiers=[
                TierOut(
                    tier=tier,
                    has_bet=False,
                    reason="set a bankroll in settings — every stake is a share of it",
                )
                for tier in DEFAULT_TIERS
            ],
            bankroll=0.0,
            currency=settings.currency,
            staking_note=drawdown_warning(settings.kelly_multiplier),
            slate_start=start,
            slate_end=end,
        )

    candidates = await build_candidates(
        session,
        start=start,
        end=end,
        sport=sport,
        league=league,
        min_confidence=min_confidence,
    )
    exposure = await exposure_summary(session, bankroll)
    results = build_all_tiers(candidates, bankroll, remaining_exposure=exposure.remaining_daily)

    tiers = [await _tier_out(session, result, settings.currency) for result in results]
    return BuilderResponse(
        tiers=tiers,
        bankroll=float(bankroll),
        currency=settings.currency,
        staking_note=drawdown_warning(settings.kelly_multiplier),
        slate_start=start,
        slate_end=end,
    )


@router.get("")
async def list_bets(
    session: SessionDep,
    status: BetStatus | None = None,
    limit: int = 100,
) -> list[TrackedBetOut]:
    stmt = select(Bet).order_by(Bet.created_at.desc()).limit(min(limit, 500))
    if status:
        stmt = stmt.where(Bet.status == status)
    bets = (await session.execute(stmt)).scalars().all()
    return [TrackedBetOut.model_validate(bet) for bet in bets]


@router.post("")
async def track_bet(body: TrackBetIn, session: SessionDep) -> TrackedBetOut:
    """Log a bet as placed, so the tracker can grade it against real outcomes."""
    settings = get_settings()
    bet = Bet(
        tier=body.tier,
        status=BetStatus.PLACED,
        stake=Decimal(str(body.stake)),
        currency=settings.currency,
        combined_price_decimal=Decimal(str(body.combined_price_decimal)),
        combined_probability=body.combined_probability,
        naive_probability=body.naive_probability,
        expected_value=body.expected_value,
        kelly_fraction=body.kelly_fraction,
        bankroll_at_placement=Decimal(str(settings.bankroll)),
        placed_at=datetime.now(UTC),
    )
    session.add(bet)
    await session.flush()

    for leg in body.legs:
        fixture_id = leg.get("fixture_id")
        if fixture_id is None:
            raise HTTPException(status_code=422, detail="each leg needs a fixture_id")
        session.add(
            BetLeg(
                bet_id=bet.id,
                fixture_id=int(fixture_id),
                market=str(leg.get("market", "")),
                selection=str(leg.get("selection", "")),
                line=Decimal(str(leg["line"])) if leg.get("line") is not None else None,
                price_decimal=Decimal(str(leg.get("price_decimal", 1))),
                bookmaker=leg.get("bookmaker"),
                model_probability=leg.get("model_probability"),
                implied_probability=leg.get("implied_probability"),
                edge=leg.get("edge"),
            )
        )
    await session.commit()
    return TrackedBetOut.model_validate(bet)


async def _tier_out(session: SessionDep, result: TierResult, currency: str) -> TierOut:
    if not result.has_bet or result.price is None or result.stake is None:
        return TierOut(
            tier=result.tier,
            has_bet=False,
            candidates_considered=result.candidates_considered,
            candidates_qualifying=result.candidates_qualifying,
            reason=result.reason,
        )

    legs = [await _leg_out(session, leg) for leg in result.legs]
    price = result.price
    stake = result.stake
    return TierOut(
        tier=result.tier,
        has_bet=True,
        legs=legs,
        combined_odds=price.combined_odds,
        combined_probability=price.combined_probability,
        naive_probability=price.naive_probability,
        correlation_effect=price.correlation_effect,
        expected_value=price.expected_value,
        edge=price.edge,
        stake=float(stake.stake),
        stake_fraction=stake.fraction_of_bankroll,
        kelly_fraction=stake.kelly_fraction,
        capped_by=stake.capped_by,
        projected_return=float(stake.stake) * price.combined_odds,
        loss_frequency=loss_frequency_phrase(price.combined_probability),
        candidates_considered=result.candidates_considered,
        candidates_qualifying=result.candidates_qualifying,
        copy_text=_copy_text(legs, price.combined_odds, float(stake.stake), currency),
    )


async def _leg_out(session: SessionDep, leg: Leg) -> LegOut:
    fixture = await session.get(Fixture, leg.fixture_id)
    league = await session.get(League, fixture.league_id) if fixture else None
    name = await _fixture_name(session, leg.fixture_id)
    return LegOut(
        fixture_id=leg.fixture_id,
        fixture=name,
        league=league.name if league else "",
        kick_off=fixture.start_time if fixture else datetime.now(UTC),
        market=leg.market,
        selection=leg.selection,
        line=float(leg.line) if leg.line is not None else None,
        price_decimal=leg.decimal_odds,
        bookmaker=leg.bookmaker,
        model_probability=leg.model_probability,
        implied_probability=leg.devigged_probability,
        edge=leg.edge,
        confidence=leg.confidence,
        reasoning=_reasoning(leg),
    )


async def _fixture_name(session: SessionDep, fixture_id: int) -> str:
    from app.db.enums import Side
    from app.db.models import FixtureParticipant, Team

    participants = (
        (
            await session.execute(
                select(FixtureParticipant).where(FixtureParticipant.fixture_id == fixture_id)
            )
        )
        .scalars()
        .all()
    )
    names = {}
    for participant in participants:
        if participant.team_id:
            team = await session.get(Team, participant.team_id)
            names[participant.side] = team.name if team else "?"
    return f"{names.get(Side.HOME, '?')} v {names.get(Side.AWAY, '?')}"


def _reasoning(leg: Leg) -> str:
    """Per-leg explanation for the 'why these legs' expander."""
    if leg.edge is None:
        return (
            f"model probability {leg.model_probability:.1%}; no de-vigged market price to compare"
        )
    return (
        f"model {leg.model_probability:.1%} against a de-vigged market price of "
        f"{leg.devigged_probability:.1%} — an edge of {leg.edge:+.1%} at {leg.decimal_odds:.2f}"
    )


def _copy_text(legs: list[LegOut], odds: float, stake: float, currency: str) -> str:
    lines = [
        f"{leg.fixture}: {leg.market} {leg.selection}"
        + (f" {leg.line}" if leg.line is not None else "")
        + f" @ {leg.price_decimal:.2f}"
        + (f" ({leg.bookmaker})" if leg.bookmaker else "")
        for leg in legs
    ]
    lines.append(f"Combined odds: {odds:.2f}")
    lines.append(f"Stake: {currency} {stake:.2f}")
    return "\n".join(lines)
