"""Walk-forward backtesting.

The whole value of a backtest rests on one property: **no lookahead**. A model
that has seen the result it is predicting will look extraordinary and lose
money live. So the harness enforces the rule structurally rather than trusting
the caller:

- training data is filtered to fixtures that *kicked off before* the cutoff;
- odds are read from snapshots captured before kickoff, never the settled price;
- the walk moves forward one window at a time, refitting on the past only.

`LookaheadError` is raised if a candidate ever carries data from at or after
its own fixture's kickoff. It is better for a backtest to fail loudly than to
report a fictional edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.betting.odds import DevigMethod, devig
from app.betting.staking import recommend_stake
from app.core.logging import get_logger
from app.db.enums import Confidence, FixtureStatus, Outcome, Side
from app.db.models import Fixture, FixtureParticipant, League, OddsSnapshot, Result
from app.models import football
from app.models.elo import EloModel
from app.scoring.metrics import (
    Interval,
    brier_score,
    calibration,
    equity_curve,
    hit_rate,
    log_loss,
    roi,
)
from app.scoring.settlement import grade_selection, payout_multiplier
from app.sim.engine import ScoreSimulator
from app.sim.markets import football_markets

log = get_logger(__name__)


class LookaheadError(RuntimeError):
    """Data from at or after kickoff reached a prediction for that fixture."""


@dataclass
class BacktestBet:
    fixture_id: int
    kick_off: datetime
    market: str
    selection: str
    line: Decimal | None
    model_probability: float
    devigged_probability: float
    price: float
    stake: Decimal
    outcome: Outcome
    payout: Decimal

    @property
    def edge(self) -> float:
        return self.model_probability - self.devigged_probability


@dataclass
class BacktestResult:
    league: str
    model_version: str
    start: date
    end: date
    windows: int
    fixtures_predicted: int
    bets_placed: int
    hit_rate: Interval
    roi: Interval
    brier_score: float | None
    log_loss: float | None
    calibration: list[dict[str, float]]
    final_bankroll: float
    max_drawdown: float
    longest_losing_streak: int
    bets: list[BacktestBet] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


async def run_backtest(
    session: AsyncSession,
    league_slug: str,
    start: date,
    end: date,
    *,
    starting_bankroll: float = 1000.0,
    window_days: int = 7,
    min_edge: float = 0.03,
    min_training_matches: int = 40,
    kelly_multiplier: float = 0.25,
    max_stake_fraction: float = 0.02,
    n_iterations: int = 5_000,
) -> BacktestResult:
    """Replay a period, refitting weekly on data available at the time."""
    league = (
        await session.execute(select(League).where(League.slug == league_slug))
    ).scalar_one_or_none()
    if league is None:
        raise ValueError(f"unknown league '{league_slug}'")

    simulator = ScoreSimulator(n_iterations)
    bankroll = Decimal(str(starting_bankroll))
    bets: list[BacktestBet] = []
    notes: list[str] = []
    windows = fixtures_predicted = 0
    probabilities: list[float] = []
    binary: list[int] = []

    cursor = start
    while cursor < end:
        window_end = min(cursor + timedelta(days=window_days), end)
        windows += 1

        history, elo = await _training_data(session, league.id, cursor)
        if len(history) < min_training_matches:
            notes.append(
                f"{cursor.isoformat()}: only {len(history)} finished matches available — "
                "not enough to fit, window skipped"
            )
            cursor = window_end
            continue

        params = football.fit(history, as_of=cursor)
        fixtures = await _fixtures_in_window(session, league.id, cursor, window_end)

        for fixture, home_id, away_id, result in fixtures:
            _assert_no_lookahead(history, fixture.start_time)
            fixtures_predicted += 1

            projection = football.project(params, home_id, away_id)
            if projection.confidence == Confidence.LOW:
                continue

            sim = simulator.simulate_poisson(
                fixture_id=fixture.id,
                model_version_id=0,
                home_lambda=projection.home_lambda,
                away_lambda=projection.away_lambda,
                rho=projection.rho,
                seed=fixture.id,
            )
            outcomes = {(o.market, o.selection, o.line): o for o in football_markets(sim)}

            prices = await _prices_before_kickoff(session, fixture.id, fixture.start_time)
            for (market, line), selections in prices.items():
                names = list(selections)
                fair = devig([selections[n] for n in names], method=DevigMethod.POWER)
                for name, fair_probability in zip(names, fair, strict=True):
                    outcome = outcomes.get((market, name, line))
                    if outcome is None:
                        continue

                    edge = outcome.probability - fair_probability
                    if edge < min_edge:
                        continue

                    price = selections[name]
                    stake = recommend_stake(
                        outcome.probability,
                        price,
                        bankroll,
                        kelly_multiplier=kelly_multiplier,
                        max_fraction=max_stake_fraction,
                    )
                    if stake.is_zero:
                        continue

                    graded = grade_selection(
                        market, name, line, result.home_score or 0, result.away_score or 0
                    )
                    payout = (
                        stake.stake * Decimal(str(payout_multiplier(graded, price)))
                    ).quantize(Decimal("0.01"))
                    bankroll += payout - stake.stake

                    bets.append(
                        BacktestBet(
                            fixture_id=fixture.id,
                            kick_off=fixture.start_time,
                            market=market,
                            selection=name,
                            line=line,
                            model_probability=outcome.probability,
                            devigged_probability=fair_probability,
                            price=price,
                            stake=stake.stake,
                            outcome=graded,
                            payout=payout,
                        )
                    )
                    probabilities.append(outcome.probability)
                    binary.append(1 if graded in {Outcome.WIN, Outcome.HALF_WIN} else 0)

        cursor = window_end

    stakes = [b.stake for b in bets]
    payouts = [b.payout for b in bets]
    curve = equity_curve(starting_bankroll, stakes, payouts)

    if not bets:
        notes.append(
            "No bets qualified across the whole period. That is a real result: it "
            "means the model never disagreed with the market by enough to act on."
        )

    return BacktestResult(
        league=league_slug,
        model_version=football.MODEL_VERSION,
        start=start,
        end=end,
        windows=windows,
        fixtures_predicted=fixtures_predicted,
        bets_placed=len(bets),
        hit_rate=hit_rate([b.outcome for b in bets]),
        roi=roi(stakes, payouts),
        brier_score=brier_score(probabilities, binary) if probabilities else None,
        log_loss=log_loss(probabilities, binary) if probabilities else None,
        calibration=[
            {
                "predicted": bucket.predicted,
                "actual": bucket.actual,
                "count": float(bucket.count),
            }
            for bucket in calibration(probabilities, binary)
        ],
        final_bankroll=float(bankroll),
        max_drawdown=curve.max_drawdown,
        longest_losing_streak=curve.longest_losing_streak,
        bets=bets,
        notes=notes,
    )


def _assert_no_lookahead(history: list[football.Match], kick_off: datetime) -> None:
    """The structural guarantee: nothing in training may postdate kickoff."""
    cutoff = kick_off.date()
    for match in history:
        if match.played_on >= cutoff:
            raise LookaheadError(
                f"training data contains a match played {match.played_on}, at or after "
                f"the {cutoff} fixture being predicted"
            )


async def _training_data(
    session: AsyncSession, league_id: int, cutoff: date
) -> tuple[list[football.Match], EloModel]:
    """Finished matches strictly before the cutoff."""
    cutoff_dt = datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC)
    stmt = (
        select(Fixture, Result)
        .join(Result, Result.fixture_id == Fixture.id)
        .where(
            Fixture.league_id == league_id,
            Fixture.status == FixtureStatus.FINISHED,
            Fixture.start_time < cutoff_dt,
        )
        .order_by(Fixture.start_time)
    )
    rows = (await session.execute(stmt)).all()

    matches: list[football.Match] = []
    elo = EloModel()
    for fixture, result in rows:
        sides = await _sides(session, fixture.id)
        if sides is None or result.home_score is None or result.away_score is None:
            continue
        matches.append(
            football.Match(
                home_id=sides[0],
                away_id=sides[1],
                home_goals=result.home_score,
                away_goals=result.away_score,
                played_on=fixture.start_time.date(),
            )
        )
        elo.update(sides[0], sides[1], result.home_score, result.away_score)
    return matches, elo


async def _fixtures_in_window(
    session: AsyncSession, league_id: int, start: date, end: date
) -> list[tuple[Fixture, int, int, Result]]:
    """Finished fixtures in the window, with their results for grading."""
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(end, datetime.min.time(), tzinfo=UTC)
    stmt = (
        select(Fixture, Result)
        .join(Result, Result.fixture_id == Fixture.id)
        .where(
            Fixture.league_id == league_id,
            Fixture.status == FixtureStatus.FINISHED,
            Fixture.start_time >= start_dt,
            Fixture.start_time < end_dt,
        )
        .order_by(Fixture.start_time)
    )
    rows = (await session.execute(stmt)).all()

    out = []
    for fixture, result in rows:
        sides = await _sides(session, fixture.id)
        if sides is not None:
            out.append((fixture, sides[0], sides[1], result))
    return out


async def _prices_before_kickoff(
    session: AsyncSession, fixture_id: int, kick_off: datetime
) -> dict[tuple[str, Decimal | None], dict[str, float]]:
    """Latest price per selection captured strictly before kickoff.

    Using any snapshot at or after kickoff would leak the result into the
    price — the subtlest and most common way a backtest lies.
    """
    stmt = (
        select(OddsSnapshot)
        .where(
            OddsSnapshot.fixture_id == fixture_id,
            OddsSnapshot.captured_at < kick_off,
        )
        .order_by(OddsSnapshot.captured_at.desc())
    )
    snapshots = (await session.execute(stmt)).scalars().all()

    prices: dict[tuple[str, Decimal | None], dict[str, float]] = {}
    for snapshot in snapshots:
        market = prices.setdefault((snapshot.market, snapshot.line), {})
        if snapshot.selection not in market:  # newest wins
            market[snapshot.selection] = float(snapshot.price_decimal)
    return prices


async def _sides(session: AsyncSession, fixture_id: int) -> tuple[int, int] | None:
    participants = (
        (
            await session.execute(
                select(FixtureParticipant).where(FixtureParticipant.fixture_id == fixture_id)
            )
        )
        .scalars()
        .all()
    )
    home = next((p.team_id for p in participants if p.side == Side.HOME and p.team_id), None)
    away = next((p.team_id for p in participants if p.side == Side.AWAY and p.team_id), None)
    return (home, away) if home and away else None


def compare_results(a: BacktestResult, b: BacktestResult) -> dict[str, object]:
    """Compare two backtests on the same slate.

    Reports whether the intervals overlap, because two ROIs differing by a few
    points over a hundred bets are not distinguishable and should not be
    treated as a ranking.
    """
    overlap = not (a.roi.high < b.roi.low or b.roi.high < a.roi.low)
    return {
        "a": {"model": a.model_version, "roi": a.roi.point, "n": a.roi.n},
        "b": {"model": b.model_version, "roi": b.roi.point, "n": b.roi.n},
        "roi_difference": a.roi.point - b.roi.point,
        "intervals_overlap": overlap,
        "verdict": (
            "indistinguishable at this sample size"
            if overlap
            else f"{'A' if a.roi.point > b.roi.point else 'B'} performed better"
        ),
    }
