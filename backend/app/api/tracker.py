"""Performance tracking: hit rate, ROI, calibration, CLV, equity curve.

Every rate carries a confidence interval and its sample size, because the
alternative — a bare "58% hit rate" over 40 bets — reads as evidence when it is
noise.
"""

from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import SessionDep
from app.core.config import get_settings
from app.db.enums import BetStatus, BetTier, Outcome
from app.db.models import Bet, BetLeg, Prediction, Settlement
from app.scoring.ledger import exposure_summary
from app.scoring.metrics import (
    Interval,
    brier_score,
    calibration,
    closing_line_value,
    equity_curve,
    hit_rate,
    log_loss,
    outcome_to_binary,
    roi,
)
from app.scoring.settlement import Settler

router = APIRouter(prefix="/tracker", tags=["tracker"])


class IntervalOut(BaseModel):
    point: float
    low: float
    high: float
    n: int
    is_meaningful: bool
    description: str

    @classmethod
    def of(cls, interval: Interval, unit: str = "") -> "IntervalOut":
        return cls(
            point=interval.point,
            low=interval.low,
            high=interval.high,
            n=interval.n,
            is_meaningful=interval.is_meaningful,
            description=interval.describe(unit),
        )


class CalibrationOut(BaseModel):
    label: str
    predicted: float
    actual: float
    count: int


class EquityPointOut(BaseModel):
    index: int
    bankroll: float
    change: float


class MetricsResponse(BaseModel):
    hit_rate: IntervalOut
    roi: IntervalOut
    brier_score: float | None
    log_loss: float | None
    calibration: list[CalibrationOut]
    equity: list[EquityPointOut]
    max_drawdown: float
    longest_losing_streak: int
    sharpe: float | None
    average_clv: float | None
    settled_bets: int
    open_bets: int
    graded_predictions: int
    exposure: str
    sample_warning: str | None


class SettlementRunOut(BaseModel):
    predictions: int
    bets: int


@router.get("/metrics")
async def metrics(
    session: SessionDep,
    tier: BetTier | None = None,
    sport: str | None = None,
) -> MetricsResponse:
    settings = get_settings()

    stmt = select(Bet).where(Bet.status.in_([BetStatus.SETTLED, BetStatus.VOID]))
    if tier:
        stmt = stmt.where(Bet.tier == tier)
    settled = (await session.execute(stmt.order_by(Bet.settled_at))).scalars().all()

    stakes: list[Decimal] = []
    payouts: list[Decimal] = []
    outcomes: list[Outcome] = []
    for bet in settled:
        if bet.payout is None:
            continue
        stakes.append(bet.stake)
        payouts.append(bet.payout)
        outcomes.append(Outcome.WIN if bet.payout > bet.stake else Outcome.LOSE)

    # Calibration uses every graded prediction, not only the bet ones — the
    # sample is far larger and free of selection bias.
    graded = (
        await session.execute(
            select(Prediction, Settlement).join(
                Settlement, Settlement.prediction_id == Prediction.id
            )
        )
    ).all()
    probabilities: list[float] = []
    binary: list[int] = []
    for prediction, settlement in graded:
        value = outcome_to_binary(settlement.outcome)
        if value is not None:
            probabilities.append(prediction.probability)
            binary.append(value)

    curve = equity_curve(settings.bankroll, stakes, payouts)
    open_count = len(
        (await session.execute(select(Bet).where(Bet.status == BetStatus.PLACED))).scalars().all()
    )
    exposure = await exposure_summary(session)
    hit = hit_rate(outcomes)

    return MetricsResponse(
        hit_rate=IntervalOut.of(hit),
        roi=IntervalOut.of(roi(stakes, payouts)),
        brier_score=brier_score(probabilities, binary) if probabilities else None,
        log_loss=log_loss(probabilities, binary) if probabilities else None,
        calibration=[
            CalibrationOut(label=b.label, predicted=b.predicted, actual=b.actual, count=b.count)
            for b in calibration(probabilities, binary)
        ],
        equity=[
            EquityPointOut(index=p.index, bankroll=p.bankroll, change=p.change)
            for p in curve.points
        ],
        max_drawdown=curve.max_drawdown,
        longest_losing_streak=curve.longest_losing_streak,
        sharpe=curve.sharpe,
        average_clv=await _average_clv(session),
        settled_bets=len(settled),
        open_bets=open_count,
        graded_predictions=len(probabilities),
        exposure=exposure.describe(),
        sample_warning=_sample_warning(hit),
    )


@router.post("/settle")
async def run_settlement(session: SessionDep) -> SettlementRunOut:
    """Grade everything whose fixture has finished."""
    counts = await Settler(session).settle_all()
    return SettlementRunOut(**counts)


async def _average_clv(session: SessionDep) -> float | None:
    """Mean closing line value across settled legs that have a closing price."""
    from app.ingest.odds import closing_line

    legs = (
        (
            await session.execute(
                select(BetLeg)
                .join(Bet, BetLeg.bet_id == Bet.id)
                .where(Bet.status == BetStatus.SETTLED)
            )
        )
        .scalars()
        .all()
    )
    values = []
    for leg in legs:
        snapshot = await closing_line(session, leg.fixture_id, leg.market, leg.selection, leg.line)
        if snapshot is None:
            continue
        values.append(closing_line_value(float(leg.price_decimal), float(snapshot.price_decimal)))
    return sum(values) / len(values) if values else None


def _sample_warning(hit: Interval) -> str | None:
    if hit.n == 0:
        return "No settled bets yet — there is nothing to conclude from."
    if hit.n < 30:
        return (
            f"Only {hit.n} settled bets. At this sample size the results are "
            "indistinguishable from chance; treat them as a sanity check on the "
            "plumbing, not evidence of an edge."
        )
    if hit.n < 200:
        return (
            f"{hit.n} settled bets is still a small sample. Closing line value is a "
            "more reliable early signal than hit rate."
        )
    return None
