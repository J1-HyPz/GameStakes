"""Odds conversion and margin removal.

De-vigging matters more than it looks. The naive approach — divide each implied
probability by their sum — assumes the bookmaker spreads margin evenly across
outcomes. They do not: longshots carry disproportionately more margin
(favourite-longshot bias), so proportional de-vigging systematically
*overstates* the fair probability of longshots and understates favourites.
A model compared against those numbers will think it has an edge on longshots
that does not exist.

The power method is used by default: it solves for the exponent k such that
sum(p_i^k) = 1, which shrinks longshots harder than favourites, matching
observed bookmaker behaviour. Shin's method is also provided — it models the
margin as arising from insider trading and is a common alternative.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from fractions import Fraction

from scipy.optimize import brentq


class OddsFormat(enum.StrEnum):
    DECIMAL = "decimal"
    FRACTIONAL = "fractional"
    AMERICAN = "american"


class DevigMethod(enum.StrEnum):
    POWER = "power"
    SHIN = "shin"
    PROPORTIONAL = "proportional"  # available, but biased; not the default


def decimal_to_american(decimal_odds: float) -> int:
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1) * 100)
    return round(-100 / (decimal_odds - 1))


def american_to_decimal(american: int) -> float:
    if american > 0:
        return 1 + american / 100
    return 1 + 100 / abs(american)


def decimal_to_fractional(decimal_odds: float, max_denominator: int = 100) -> str:
    fraction = Fraction(decimal_odds - 1).limit_denominator(max_denominator)
    return f"{fraction.numerator}/{fraction.denominator}"


def fractional_to_decimal(fractional: str) -> float:
    numerator, _, denominator = fractional.partition("/")
    return 1 + float(numerator) / float(denominator)


def format_odds(decimal_odds: float, fmt: OddsFormat) -> str:
    if fmt is OddsFormat.DECIMAL:
        return f"{decimal_odds:.2f}"
    if fmt is OddsFormat.AMERICAN:
        american = decimal_to_american(decimal_odds)
        return f"+{american}" if american > 0 else str(american)
    return decimal_to_fractional(decimal_odds)


def implied_probability(decimal_odds: float) -> float:
    """Raw implied probability, margin included."""
    if decimal_odds <= 1.0:
        raise ValueError("decimal odds must exceed 1.0")
    return 1.0 / decimal_odds


def overround(decimal_odds: list[float]) -> float:
    """Bookmaker margin: how far implied probabilities sum beyond 1."""
    return sum(implied_probability(o) for o in decimal_odds) - 1.0


def devig(decimal_odds: list[float], method: DevigMethod = DevigMethod.POWER) -> list[float]:
    """Fair probabilities summing to 1, with the bookmaker margin removed."""
    if not decimal_odds:
        return []
    raw = [implied_probability(o) for o in decimal_odds]
    total = sum(raw)
    if total <= 1.0:
        # No margin (or a genuine arbitrage): normalise and move on.
        return [p / total for p in raw]

    if method is DevigMethod.PROPORTIONAL:
        return [p / total for p in raw]
    if method is DevigMethod.SHIN:
        return _shin(raw)
    return _power(raw)


def _power(raw: list[float]) -> list[float]:
    """Solve sum(p_i^k) = 1 for k > 1, shrinking longshots more than favourites."""

    def excess(k: float) -> float:
        return float(sum(p**k for p in raw)) - 1.0

    try:
        k = float(brentq(excess, 1.0, 20.0, xtol=1e-10))
    except ValueError:
        # No root in range (extreme books): fall back rather than fail.
        total = sum(raw)
        return [p / total for p in raw]
    fair = [p**k for p in raw]
    total = sum(fair)
    return [p / total for p in fair]


def _shin(raw: list[float]) -> list[float]:
    """Shin's method: margin modelled as a share z of insider money."""
    total = sum(raw)

    def excess(z: float) -> float:
        return sum(_shin_probability(p, z, total) for p in raw) - 1.0

    try:
        z = float(brentq(excess, 1e-9, 0.4, xtol=1e-10))
    except ValueError:
        return [p / total for p in raw]
    fair = [_shin_probability(p, z, total) for p in raw]
    normaliser = sum(fair)
    return [p / normaliser for p in fair]


def _shin_probability(p: float, z: float, total: float) -> float:
    root: float = (z**2 + 4 * (1 - z) * p**2 / total) ** 0.5
    return (root - z) / (2 * (1 - z))


def fair_odds(probability: float) -> float:
    """Break-even decimal odds for a probability."""
    if not 0 < probability <= 1:
        raise ValueError("probability must be in (0, 1]")
    return 1.0 / probability


def expected_value(model_probability: float, decimal_odds: float, stake: float = 1.0) -> float:
    """EV per unit staked: p*(odds-1) - (1-p).

    A fair coin at 2.0 returns exactly zero — the known-answer case the tests
    pin down.
    """
    return stake * (model_probability * (decimal_odds - 1) - (1 - model_probability))


def edge(model_probability: float, devigged_probability: float) -> float:
    """How far the model's probability exceeds the fair market price."""
    return model_probability - devigged_probability


def best_price(prices: dict[str, float]) -> tuple[str, float]:
    """Line shopping: the bookmaker offering the highest decimal odds."""
    if not prices:
        raise ValueError("no prices to compare")
    bookmaker = max(prices, key=lambda book: prices[book])
    return bookmaker, prices[bookmaker]


def to_decimal(value: float) -> Decimal:
    """Money-safe conversion for storage."""
    return Decimal(str(round(value, 3)))
