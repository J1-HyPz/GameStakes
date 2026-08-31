"""Stake sizing.

Kelly maximises long-run growth *if* the probability is exactly right. It never
is: a model that is 3% optimistic turns full Kelly into a losing strategy, and
even a correct model produces drawdowns most people cannot sit through. So
fractional Kelly is the default, the fraction is a setting, and every stake is
capped as a share of bankroll regardless of what the maths suggests.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class StakeRecommendation:
    stake: Decimal
    fraction_of_bankroll: float
    kelly_fraction: float
    full_kelly_fraction: float
    capped_by: str | None  # "tier_cap", "exposure_cap", or None
    expected_value: float

    @property
    def is_zero(self) -> bool:
        return self.stake <= 0


def kelly_fraction(probability: float, decimal_odds: float) -> float:
    """Full Kelly stake as a fraction of bankroll.

    f* = (bp - q) / b, where b = odds - 1. Negative means no bet: the edge is
    against you, and Kelly's answer to a bad bet is to not make it.
    """
    if decimal_odds <= 1.0:
        return 0.0
    b = decimal_odds - 1.0
    q = 1.0 - probability
    fraction = (b * probability - q) / b
    return max(fraction, 0.0)


def recommend_stake(
    probability: float,
    decimal_odds: float,
    bankroll: Decimal,
    *,
    kelly_multiplier: float = 0.25,
    max_fraction: float = 0.02,
    remaining_exposure: Decimal | None = None,
    min_stake: Decimal = Decimal("0.50"),
) -> StakeRecommendation:
    """Fractional Kelly, capped by tier limit and remaining daily exposure."""
    full = kelly_fraction(probability, decimal_odds)
    ev = probability * (decimal_odds - 1) - (1 - probability)

    if full <= 0:
        return StakeRecommendation(
            stake=Decimal("0"),
            fraction_of_bankroll=0.0,
            kelly_fraction=0.0,
            full_kelly_fraction=full,
            capped_by=None,
            expected_value=ev,
        )

    fraction = full * kelly_multiplier
    capped_by: str | None = None
    if fraction > max_fraction:
        fraction = max_fraction
        capped_by = "tier_cap"

    stake = (bankroll * Decimal(str(fraction))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    if remaining_exposure is not None and stake > remaining_exposure:
        stake = max(remaining_exposure, Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        capped_by = "exposure_cap"

    if stake < min_stake:
        # Below the minimum, the honest answer is no bet rather than a token one.
        stake = Decimal("0")

    actual_fraction = float(stake / bankroll) if bankroll > 0 else 0.0
    return StakeRecommendation(
        stake=stake,
        fraction_of_bankroll=actual_fraction,
        kelly_fraction=fraction,
        full_kelly_fraction=full,
        capped_by=capped_by,
        expected_value=ev,
    )


def loss_frequency_phrase(probability: float) -> str:
    """Plain-language expected loss rate, for the bet card.

    People read "62% to win" as "this wins"; "expect it to lose about 4 times
    in 10" is the same number stated so the downside registers.
    """
    lose_rate = 1.0 - probability
    if lose_rate <= 0:
        return "expected to win every time — check the model, this is implausible"
    per_ten = lose_rate * 10
    if per_ten < 0.5:
        per_hundred = round(lose_rate * 100)
        return f"expect this to lose roughly {per_hundred} times in 100"
    return f"expect this to lose roughly {round(per_ten)} times in 10"


def drawdown_warning(kelly_multiplier: float) -> str:
    """Honest framing of the variance a staking plan implies."""
    if kelly_multiplier >= 0.5:
        return (
            "Half Kelly or higher: expect swings of 50%+ of bankroll even when the model is right."
        )
    if kelly_multiplier >= 0.25:
        return "Quarter Kelly: drawdowns of 20-30% are normal over a season, not a sign of failure."
    return "Conservative staking: slower growth, shallower drawdowns."
