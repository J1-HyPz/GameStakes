"""Parlay pricing from the joint distribution.

The rule this module exists to enforce: **never multiply marginal
probabilities for legs in the same game.** If a team is projected to win big,
its striker scoring and the team total going over are both more likely — the
legs are positively correlated, so the true parlay probability is *higher* than
the product for aligned legs and *lower* for opposing ones. Multiplying
marginals produces a number that looks like an edge and is not, which is
exactly how a bet builder ends up confidently unprofitable.

Legs from the same fixture are priced off shared simulation draws: the parlay
probability is the fraction of iterations in which every leg wins. Legs from
different fixtures are independent unless a shared factor is declared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np


@dataclass(frozen=True)
class Leg:
    """One selection, carrying the mask that decides it in each iteration."""

    fixture_id: int
    market: str
    selection: str
    line: Decimal | None
    decimal_odds: float
    model_probability: float
    devigged_probability: float | None
    bookmaker: str | None
    mask: np.ndarray = field(repr=False)
    player_id: int | None = None
    confidence: str = "medium"

    @property
    def edge(self) -> float | None:
        if self.devigged_probability is None:
            return None
        return self.model_probability - self.devigged_probability

    @property
    def expected_value(self) -> float:
        return self.model_probability * (self.decimal_odds - 1) - (1 - self.model_probability)


@dataclass(frozen=True)
class ParlayPrice:
    combined_probability: float
    naive_probability: float
    combined_odds: float
    expected_value: float
    correlation_effect: float  # joint - naive; positive means legs reinforce

    @property
    def fair_odds(self) -> float | None:
        return 1.0 / self.combined_probability if self.combined_probability > 0 else None

    @property
    def edge(self) -> float:
        """Model probability minus the break-even probability of the price."""
        return self.combined_probability - (1.0 / self.combined_odds)


def combined_odds(legs: list[Leg]) -> float:
    """Prices multiply — that part is arithmetic, not probability."""
    product = 1.0
    for leg in legs:
        product *= leg.decimal_odds
    return product


def price_parlay(legs: list[Leg]) -> ParlayPrice:
    """Price a parlay using the joint distribution where legs share a fixture.

    Same-fixture legs are intersected over shared draws; groups from different
    fixtures are multiplied, which is the correct treatment for genuinely
    independent events.
    """
    if not legs:
        return ParlayPrice(0.0, 0.0, 1.0, 0.0, 0.0)

    by_fixture: dict[int, list[Leg]] = {}
    for leg in legs:
        by_fixture.setdefault(leg.fixture_id, []).append(leg)

    joint = 1.0
    for fixture_legs in by_fixture.values():
        masks = [leg.mask for leg in fixture_legs]
        combined_mask = np.logical_and.reduce(masks) if len(masks) > 1 else masks[0]
        joint *= float(np.mean(combined_mask))

    naive = 1.0
    for leg in legs:
        naive *= leg.model_probability

    price = combined_odds(legs)
    ev = joint * (price - 1) - (1 - joint)
    return ParlayPrice(
        combined_probability=joint,
        naive_probability=naive,
        combined_odds=price,
        expected_value=ev,
        correlation_effect=joint - naive,
    )


def is_contradictory(legs: list[Leg]) -> bool:
    """True when no simulated world satisfies every leg.

    Catches both the obvious case (home win and away win) and the subtle one
    (over 3.5 goals with both teams to score 'no' and a 1-0 correct score),
    without needing a hand-written rule per market pair — if the intersection
    is empty across 20,000 iterations, the combination is impossible or so
    unlikely it must not be offered.
    """
    same_fixture: dict[int, list[Leg]] = {}
    for leg in legs:
        same_fixture.setdefault(leg.fixture_id, []).append(leg)

    for fixture_legs in same_fixture.values():
        if len(fixture_legs) < 2:
            continue
        combined = np.logical_and.reduce([leg.mask for leg in fixture_legs])
        if not combined.any():
            return True
    return False


def duplicate_selection(legs: list[Leg]) -> bool:
    """Same market and line twice on one fixture — a doubled position, not a parlay."""
    seen = {(leg.fixture_id, leg.market, leg.line) for leg in legs}
    return len(seen) < len(legs)
