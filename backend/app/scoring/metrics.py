"""Performance metrics.

The guiding principle: **sample size must be impossible to ignore.** A 55% hit
rate over 40 bets is noise, and a tool that reports it without an interval
invites its user to bet more on nothing. So every rate ships with a bootstrap
confidence interval and its n.

Brier score and log loss measure probability quality, which matters more than
win rate: a model that says 70% and is right 70% of the time is working, even
in a losing month. Calibration is the single most informative view, and CLV is
the earliest reliable signal that an edge is real — hit rate over a few hundred
bets is mostly variance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np

from app.db.enums import Outcome

# Outcomes that count as resolved for rate calculations. Pushes and voids
# return the stake and belong in neither numerator nor denominator.
DECIDED = {Outcome.WIN, Outcome.LOSE, Outcome.HALF_WIN, Outcome.HALF_LOSE}


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float
    n: int

    @property
    def is_meaningful(self) -> bool:
        """Whether the sample supports any conclusion at all."""
        return self.n >= 30

    def describe(self, unit: str = "") -> str:
        if self.n == 0:
            return "no settled bets yet"
        body = f"{self.point:.1%}{unit} (95% CI {self.low:.1%} to {self.high:.1%}, n={self.n})"
        if not self.is_meaningful:
            return f"{body} — too few bets to draw any conclusion"
        return body


def bootstrap_interval(values: list[float], iterations: int = 5_000, seed: int = 12345) -> Interval:
    """Percentile bootstrap around the mean.

    Bootstrap rather than a normal approximation because betting returns are
    skewed: most bets lose a little and a few win a lot, so a symmetric
    interval understates the downside.
    """
    if not values:
        return Interval(0.0, 0.0, 0.0, 0)
    array = np.asarray(values, dtype=float)
    if len(array) == 1:
        value = float(array[0])
        return Interval(value, value, value, 1)

    rng = np.random.default_rng(seed)
    samples = rng.choice(array, size=(iterations, len(array)), replace=True).mean(axis=1)
    return Interval(
        point=float(array.mean()),
        low=float(np.percentile(samples, 2.5)),
        high=float(np.percentile(samples, 97.5)),
        n=len(array),
    )


def hit_rate(outcomes: list[Outcome]) -> Interval:
    """Win rate over decided bets, with an interval."""
    decided = [o for o in outcomes if o in DECIDED]
    if not decided:
        return Interval(0.0, 0.0, 0.0, 0)
    wins = [1.0 if o == Outcome.WIN else 0.5 if o == Outcome.HALF_WIN else 0.0 for o in decided]
    return bootstrap_interval(wins)


def roi(stakes: list[Decimal], payouts: list[Decimal]) -> Interval:
    """Return on investment per bet, with an interval."""
    if not stakes:
        return Interval(0.0, 0.0, 0.0, 0)
    returns = [
        float((payout - stake) / stake)
        for stake, payout in zip(stakes, payouts, strict=True)
        if stake > 0
    ]
    return bootstrap_interval(returns)


def brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    """Mean squared error of probabilistic forecasts. Lower is better; 0.25 is
    what you get by always saying 50%."""
    if not probabilities:
        return float("nan")
    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    return float(np.mean((p - y) ** 2))


def log_loss(probabilities: list[float], outcomes: list[int], eps: float = 1e-15) -> float:
    """Penalises confident errors far more heavily than Brier does."""
    if not probabilities:
        return float("nan")
    p = np.clip(np.asarray(probabilities, dtype=float), eps, 1 - eps)
    y = np.asarray(outcomes, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


@dataclass
class CalibrationBucket:
    lower: float
    upper: float
    predicted: float
    actual: float
    count: int

    @property
    def label(self) -> str:
        return f"{self.lower:.0%}-{self.upper:.0%}"


def calibration(
    probabilities: list[float], outcomes: list[int], n_buckets: int = 10
) -> list[CalibrationBucket]:
    """Predicted probability against observed frequency, bucketed.

    A well-calibrated model's 70% picks win about 70% of the time. Systematic
    deviation here is the clearest evidence that stated probabilities cannot be
    trusted — which makes every edge computed from them fiction.
    """
    if not probabilities:
        return []
    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(outcomes, dtype=float)

    buckets = []
    edges = np.linspace(0.0, 1.0, n_buckets + 1)
    for lower, upper in zip(edges[:-1], edges[1:], strict=True):
        in_bucket = (p >= lower) & (p < upper if upper < 1.0 else p <= upper)
        count = int(in_bucket.sum())
        if count == 0:
            continue
        buckets.append(
            CalibrationBucket(
                lower=float(lower),
                upper=float(upper),
                predicted=float(p[in_bucket].mean()),
                actual=float(y[in_bucket].mean()),
                count=count,
            )
        )
    return buckets


def closing_line_value(bet_odds: float, closing_odds: float) -> float:
    """How much better the taken price was than the closing price.

    Positive CLV means you consistently got a better number than the market
    settled at, which is the earliest trustworthy sign of a real edge — it
    shows up in dozens of bets, where hit rate needs hundreds.
    """
    if closing_odds <= 1.0 or bet_odds <= 1.0:
        return 0.0
    return (1.0 / closing_odds) / (1.0 / bet_odds) - 1.0


@dataclass
class EquityPoint:
    index: int
    bankroll: float
    change: float


@dataclass
class BankrollCurve:
    points: list[EquityPoint] = field(default_factory=list)
    max_drawdown: float = 0.0
    longest_losing_streak: int = 0
    sharpe: float | None = None


def equity_curve(
    starting_bankroll: float, stakes: list[Decimal], payouts: list[Decimal]
) -> BankrollCurve:
    """Bankroll over time, with drawdown and streak statistics.

    Maximum drawdown and the longest losing run are shown because they are what
    actually ends a betting strategy: people stop during the drawdown a
    profitable model was always going to have.
    """
    bankroll = starting_bankroll
    peak = starting_bankroll
    max_drawdown = 0.0
    streak = longest_streak = 0
    points = [EquityPoint(index=0, bankroll=bankroll, change=0.0)]
    returns: list[float] = []

    for i, (stake, payout) in enumerate(zip(stakes, payouts, strict=True), start=1):
        change = float(payout - stake)
        bankroll += change
        points.append(EquityPoint(index=i, bankroll=bankroll, change=change))

        if stake > 0:
            returns.append(change / float(stake))
        peak = max(peak, bankroll)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - bankroll) / peak)

        if change < 0:
            streak += 1
            longest_streak = max(longest_streak, streak)
        elif change > 0:
            streak = 0

    sharpe = None
    if len(returns) > 1:
        mean, sd = float(np.mean(returns)), float(np.std(returns, ddof=1))
        sharpe = mean / sd * math.sqrt(len(returns)) if sd > 0 else None

    return BankrollCurve(
        points=points,
        max_drawdown=max_drawdown,
        longest_losing_streak=longest_streak,
        sharpe=sharpe,
    )


def outcome_to_binary(outcome: Outcome) -> int | None:
    """Map an outcome to 0/1 for scoring; None where it does not count."""
    if outcome == Outcome.WIN:
        return 1
    if outcome == Outcome.LOSE:
        return 0
    if outcome == Outcome.HALF_WIN:
        return 1
    if outcome == Outcome.HALF_LOSE:
        return 0
    return None  # push or void
