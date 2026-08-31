"""Risk tier definitions and the bet construction search.

Three tiers, each a complete specification of what qualifies: leg count,
per-leg probability band, minimum edge, allowed markets, Kelly fraction and
stake cap. Everything is configurable; the defaults are the ones in the build
spec.

The rule that matters most: **if nothing clears the thresholds, the tier
returns nothing and says why.** A tier that reports "no qualifying bets today —
14 candidates, none above the 4% edge threshold" is doing its job. Filling the
slot with the least-bad option available is how a tool trains its user to make
losing bets.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal

from app.betting.parlay import (
    Leg,
    ParlayPrice,
    duplicate_selection,
    is_contradictory,
    price_parlay,
)
from app.betting.staking import StakeRecommendation, recommend_stake
from app.db.enums import BetTier, Confidence

# Markets each tier may use. Longshot and exotic markets are confined to the
# high-risk tier, where their variance is priced in.
MAIN_MARKETS = {"1x2", "double_chance", "team_total_home", "team_total_away", "totals"}
EXTENDED_MARKETS = MAIN_MARKETS | {"handicap", "spreads", "btts", "clean_sheet", "player_props"}
ALL_MARKETS = EXTENDED_MARKETS | {"correct_score", "method", "rounds"}


@dataclass(frozen=True)
class TierConfig:
    tier: BetTier
    min_legs: int
    max_legs: int
    min_leg_probability: float
    max_leg_probability: float
    min_edge: float
    min_confidence: Confidence
    target_odds_low: float
    target_odds_high: float
    kelly_multiplier: float
    max_stake_fraction: float
    allowed_markets: set[str]
    max_legs_per_fixture: int = 2

    def accepts_leg(self, leg: Leg) -> bool:
        if leg.market not in self.allowed_markets:
            return False
        if not self.min_leg_probability <= leg.model_probability <= self.max_leg_probability:
            return False
        if leg.edge is None or leg.edge < self.min_edge:
            return False
        return _confidence_rank(leg.confidence) >= _confidence_rank(self.min_confidence.value)


DEFAULT_TIERS: dict[BetTier, TierConfig] = {
    BetTier.LOW: TierConfig(
        tier=BetTier.LOW,
        min_legs=1,
        max_legs=3,
        min_leg_probability=0.65,
        max_leg_probability=1.0,
        min_edge=0.02,
        min_confidence=Confidence.HIGH,
        target_odds_low=1.3,
        target_odds_high=2.0,
        kelly_multiplier=0.25,
        max_stake_fraction=0.02,
        allowed_markets=MAIN_MARKETS,
    ),
    BetTier.MEDIUM: TierConfig(
        tier=BetTier.MEDIUM,
        min_legs=3,
        max_legs=5,
        min_leg_probability=0.40,
        max_leg_probability=0.65,
        min_edge=0.04,
        min_confidence=Confidence.MEDIUM,
        target_odds_low=2.5,
        target_odds_high=6.0,
        kelly_multiplier=0.15,
        max_stake_fraction=0.01,
        allowed_markets=EXTENDED_MARKETS,
    ),
    BetTier.HIGH: TierConfig(
        tier=BetTier.HIGH,
        min_legs=5,
        max_legs=8,
        min_leg_probability=0.15,
        max_leg_probability=0.40,
        min_edge=0.07,
        min_confidence=Confidence.MEDIUM,
        target_odds_low=8.0,
        target_odds_high=1000.0,
        kelly_multiplier=0.05,
        max_stake_fraction=0.0025,
        allowed_markets=ALL_MARKETS,
    ),
}


@dataclass
class TierResult:
    """Either a bet, or an honest account of why there isn't one."""

    tier: BetTier
    legs: list[Leg] = field(default_factory=list)
    price: ParlayPrice | None = None
    stake: StakeRecommendation | None = None
    candidates_considered: int = 0
    candidates_qualifying: int = 0
    reason: str | None = None

    @property
    def has_bet(self) -> bool:
        return bool(self.legs) and self.price is not None


def build_tier(
    candidates: list[Leg],
    config: TierConfig,
    bankroll: Decimal,
    *,
    remaining_exposure: Decimal | None = None,
    beam_width: int = 40,
) -> TierResult:
    """Search for the highest-EV qualifying combination for one tier."""
    considered = len(candidates)
    qualifying = [leg for leg in candidates if config.accepts_leg(leg)]

    if not qualifying:
        return TierResult(
            tier=config.tier,
            candidates_considered=considered,
            candidates_qualifying=0,
            reason=_no_candidates_reason(candidates, config),
        )

    # Highest edge first: beam search keeps the strongest partial combinations,
    # and an exhaustive search over a few hundred candidates is intractable.
    qualifying.sort(key=lambda leg: leg.edge or 0.0, reverse=True)
    qualifying = qualifying[: max(beam_width, config.max_legs * 4)]

    best: tuple[float, list[Leg], ParlayPrice] | None = None
    for size in range(config.min_legs, config.max_legs + 1):
        if size > len(qualifying):
            break
        for combination in itertools.combinations(qualifying[:beam_width], size):
            legs = list(combination)
            if not _diversification_ok(legs, config):
                continue
            if duplicate_selection(legs) or is_contradictory(legs):
                continue

            price = price_parlay(legs)
            if price.combined_probability <= 0:
                continue
            if not config.target_odds_low <= price.combined_odds <= config.target_odds_high:
                continue
            if price.expected_value <= 0:
                continue

            if best is None or price.expected_value > best[0]:
                best = (price.expected_value, legs, price)

    if best is None:
        return TierResult(
            tier=config.tier,
            candidates_considered=considered,
            candidates_qualifying=len(qualifying),
            reason=(
                f"{len(qualifying)} legs cleared the {config.min_edge:.0%} edge threshold, "
                f"but no combination landed in the {config.target_odds_low:.1f}–"
                f"{config.target_odds_high:.1f} odds range with positive expected value"
            ),
        )

    _, legs, price = best
    stake = recommend_stake(
        price.combined_probability,
        price.combined_odds,
        bankroll,
        kelly_multiplier=config.kelly_multiplier,
        max_fraction=config.max_stake_fraction,
        remaining_exposure=remaining_exposure,
    )
    if stake.is_zero:
        return TierResult(
            tier=config.tier,
            candidates_considered=considered,
            candidates_qualifying=len(qualifying),
            reason=(
                "a qualifying bet was found, but the recommended stake rounds to zero "
                "under the current bankroll and exposure limits"
            ),
        )

    return TierResult(
        tier=config.tier,
        legs=legs,
        price=price,
        stake=stake,
        candidates_considered=considered,
        candidates_qualifying=len(qualifying),
    )


def build_all_tiers(
    candidates: list[Leg],
    bankroll: Decimal,
    *,
    tiers: dict[BetTier, TierConfig] | None = None,
    remaining_exposure: Decimal | None = None,
) -> list[TierResult]:
    configs = tiers or DEFAULT_TIERS
    return [
        build_tier(candidates, config, bankroll, remaining_exposure=remaining_exposure)
        for config in configs.values()
    ]


def _diversification_ok(legs: list[Leg], config: TierConfig) -> bool:
    """Cap legs per fixture so one game cannot decide the whole bet."""
    counts: dict[int, int] = {}
    for leg in legs:
        counts[leg.fixture_id] = counts.get(leg.fixture_id, 0) + 1
        if counts[leg.fixture_id] > config.max_legs_per_fixture:
            return False
    return True


def _no_candidates_reason(candidates: list[Leg], config: TierConfig) -> str:
    """Say which filter emptied the pool — vague emptiness is not useful."""
    if not candidates:
        return "no priced selections available for this slate"

    in_market = [leg for leg in candidates if leg.market in config.allowed_markets]
    if not in_market:
        return f"no selections in this tier's markets ({', '.join(sorted(config.allowed_markets))})"

    in_band = [
        leg
        for leg in in_market
        if config.min_leg_probability <= leg.model_probability <= config.max_leg_probability
    ]
    if not in_band:
        return (
            f"{len(in_market)} selections available, none in this tier's "
            f"{config.min_leg_probability:.0%}–{config.max_leg_probability:.0%} probability band"
        )

    with_edge = [leg for leg in in_band if (leg.edge or 0) >= config.min_edge]
    if not with_edge:
        best = max((leg.edge or 0) for leg in in_band)
        return (
            f"{len(in_band)} candidates found, none above the {config.min_edge:.0%} "
            f"edge threshold (best was {best:.1%})"
        )

    return (
        f"{len(with_edge)} candidates cleared the edge threshold but none met the "
        f"{config.min_confidence.value} confidence requirement"
    )


def _confidence_rank(confidence: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(confidence, 0)
