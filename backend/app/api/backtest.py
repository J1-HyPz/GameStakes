"""Backtesting endpoints."""

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.scoring.backtest import LookaheadError, run_backtest

router = APIRouter(prefix="/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    league: str
    start: date
    end: date
    starting_bankroll: float = 1000.0
    window_days: int = 7
    min_edge: float = 0.03
    kelly_multiplier: float = 0.25


class IntervalOut(BaseModel):
    point: float
    low: float
    high: float
    n: int
    description: str


class BacktestOut(BaseModel):
    league: str
    model_version: str
    start: date
    end: date
    windows: int
    fixtures_predicted: int
    bets_placed: int
    hit_rate: IntervalOut
    roi: IntervalOut
    brier_score: float | None
    log_loss: float | None
    calibration: list[dict[str, float]]
    final_bankroll: float
    max_drawdown: float
    longest_losing_streak: int
    notes: list[str]


@router.post("/run")
async def run(body: BacktestRequest, session: SessionDep) -> BacktestOut:
    """Replay a period with walk-forward refitting.

    Training data is restricted to fixtures that kicked off before each cutoff
    and odds to snapshots captured before kickoff. If anything violates that,
    the run fails rather than reporting a fictional edge.
    """
    if body.end <= body.start:
        raise HTTPException(status_code=422, detail="`end` must be after `start`")

    try:
        result = await run_backtest(
            session,
            body.league,
            body.start,
            body.end,
            starting_bankroll=body.starting_bankroll,
            window_days=body.window_days,
            min_edge=body.min_edge,
            kelly_multiplier=body.kelly_multiplier,
        )
    except LookaheadError as exc:
        raise HTTPException(
            status_code=500, detail=f"backtest aborted — lookahead detected: {exc}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return BacktestOut(
        league=result.league,
        model_version=result.model_version,
        start=result.start,
        end=result.end,
        windows=result.windows,
        fixtures_predicted=result.fixtures_predicted,
        bets_placed=result.bets_placed,
        hit_rate=IntervalOut(
            point=result.hit_rate.point,
            low=result.hit_rate.low,
            high=result.hit_rate.high,
            n=result.hit_rate.n,
            description=result.hit_rate.describe(),
        ),
        roi=IntervalOut(
            point=result.roi.point,
            low=result.roi.low,
            high=result.roi.high,
            n=result.roi.n,
            description=result.roi.describe(),
        ),
        brier_score=result.brier_score,
        log_loss=result.log_loss,
        calibration=result.calibration,
        final_bankroll=result.final_bankroll,
        max_drawdown=result.max_drawdown,
        longest_losing_streak=result.longest_losing_streak,
        notes=result.notes,
    )
