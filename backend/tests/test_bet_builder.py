"""Tier construction, settlement grading and metrics.

The load-bearing behaviour under test: a tier with nothing qualifying returns
nothing and explains why, rather than filling the slot with the least-bad
option available.
"""

from decimal import Decimal

import numpy as np
import pytest

from app.betting.parlay import Leg
from app.betting.tiers import DEFAULT_TIERS, build_all_tiers, build_tier
from app.db.enums import BetTier, Outcome
from app.scoring.metrics import (
    bootstrap_interval,
    brier_score,
    calibration,
    closing_line_value,
    equity_curve,
    hit_rate,
    log_loss,
    outcome_to_binary,
    roi,
)
from app.scoring.settlement import grade_selection, payout_multiplier

N = 10_000
RNG = np.random.default_rng(7)


def _mask(probability: float) -> np.ndarray:
    """A mask that is true with the given probability."""
    return RNG.random(N) < probability


def _leg(
    *,
    fixture_id: int = 1,
    market: str = "1x2",
    probability: float = 0.7,
    odds: float = 1.7,
    devigged: float | None = None,
    confidence: str = "high",
    mask: np.ndarray | None = None,
) -> Leg:
    resolved = mask if mask is not None else _mask(probability)
    return Leg(
        fixture_id=fixture_id,
        market=market,
        selection=f"sel-{fixture_id}-{market}",
        line=None,
        decimal_odds=odds,
        model_probability=float(np.mean(resolved)),
        devigged_probability=(
            devigged if devigged is not None else float(np.mean(resolved)) - 0.05
        ),
        bookmaker="test",
        mask=resolved,
        confidence=confidence,
    )


class TestTierConstruction:
    def test_empty_slate_returns_no_bet_and_says_so(self) -> None:
        result = build_tier([], DEFAULT_TIERS[BetTier.LOW], Decimal("1000"))
        assert not result.has_bet
        assert result.reason == "no priced selections available for this slate"

    def test_no_edge_returns_nothing_rather_than_the_least_bad_option(self) -> None:
        """The central guardrail: a marginal bet is worse than no bet."""
        legs = [_leg(fixture_id=i, probability=0.70, odds=1.5, devigged=0.70) for i in range(1, 6)]
        result = build_tier(legs, DEFAULT_TIERS[BetTier.LOW], Decimal("1000"))

        assert not result.has_bet
        assert result.reason is not None
        assert "edge threshold" in result.reason
        assert result.candidates_considered == 5

    def test_reason_names_the_filter_that_emptied_the_pool(self) -> None:
        # All legs are in the low tier's probability band but the wrong market.
        legs = [_leg(fixture_id=i, market="correct_score", probability=0.7) for i in range(3)]
        result = build_tier(legs, DEFAULT_TIERS[BetTier.LOW], Decimal("1000"))
        assert result.reason is not None
        assert "markets" in result.reason

    def test_probability_band_is_enforced_per_tier(self) -> None:
        """A 70% leg belongs to the low tier, not the high one."""
        legs = [_leg(fixture_id=i, probability=0.70, odds=2.0) for i in range(1, 9)]
        high = build_tier(legs, DEFAULT_TIERS[BetTier.HIGH], Decimal("1000"))
        assert not high.has_bet
        assert high.reason is not None
        assert "probability band" in high.reason

    def test_low_confidence_legs_are_excluded_by_default(self) -> None:
        legs = [
            _leg(fixture_id=i, probability=0.72, odds=1.6, devigged=0.60, confidence="low")
            for i in range(1, 5)
        ]
        result = build_tier(legs, DEFAULT_TIERS[BetTier.LOW], Decimal("1000"))
        assert not result.has_bet

    def test_qualifying_legs_produce_a_bet_with_a_stake(self) -> None:
        legs = [_leg(fixture_id=i, probability=0.72, odds=1.65, devigged=0.62) for i in range(1, 4)]
        result = build_tier(legs, DEFAULT_TIERS[BetTier.LOW], Decimal("1000"))

        assert result.has_bet
        assert result.price is not None and result.stake is not None
        assert result.price.combined_odds >= DEFAULT_TIERS[BetTier.LOW].target_odds_low
        assert result.stake.stake > 0
        assert result.stake.stake <= Decimal("20.00")  # 2% cap of 1000

    def test_stake_never_exceeds_the_tier_cap(self) -> None:
        legs = [_leg(fixture_id=i, probability=0.80, odds=1.9, devigged=0.55) for i in range(1, 4)]
        result = build_tier(legs, DEFAULT_TIERS[BetTier.LOW], Decimal("10000"))
        if result.has_bet:
            assert result.stake is not None
            assert result.stake.fraction_of_bankroll <= 0.02 + 1e-9

    def test_legs_per_fixture_are_capped(self) -> None:
        """One game must not decide the whole bet."""
        legs = [
            _leg(fixture_id=1, market=f"m{i}", probability=0.72, odds=1.6, devigged=0.62)
            for i in range(6)
        ]
        result = build_tier(legs, DEFAULT_TIERS[BetTier.LOW], Decimal("1000"))
        if result.has_bet:
            assert len(result.legs) <= DEFAULT_TIERS[BetTier.LOW].max_legs_per_fixture

    def test_contradictory_legs_are_never_combined(self) -> None:
        """Two mutually exclusive legs may each qualify on their own, but the
        builder must never put both in the same bet — the parlay could not win.

        Both sit in the medium tier's 40-65% band, so each is individually
        eligible; only the contradiction check keeps them apart.
        """
        base = _mask(0.55)
        contradictory_pair = [
            _leg(fixture_id=1, market="1x2", odds=2.2, devigged=0.45, mask=base),
            _leg(fixture_id=1, market="btts", odds=2.6, devigged=0.35, mask=~base),
        ]
        # Filler legs so a qualifying 3-leg combination exists at all.
        others = [
            _leg(fixture_id=i, market="1x2", probability=0.55, odds=2.2, devigged=0.45)
            for i in range(2, 6)
        ]

        result = build_tier(
            contradictory_pair + others, DEFAULT_TIERS[BetTier.MEDIUM], Decimal("1000")
        )

        if result.has_bet:
            chosen = {(leg.fixture_id, leg.market) for leg in result.legs}
            assert not {(1, "1x2"), (1, "btts")} <= chosen
            assert result.price is not None and result.price.combined_probability > 0

    def test_zero_bankroll_yields_no_stake(self) -> None:
        legs = [_leg(fixture_id=i, probability=0.72, odds=1.65, devigged=0.62) for i in range(3)]
        result = build_tier(legs, DEFAULT_TIERS[BetTier.LOW], Decimal("0"))
        assert not result.has_bet

    def test_all_three_tiers_are_always_reported(self) -> None:
        results = build_all_tiers([], Decimal("1000"))
        assert [r.tier for r in results] == [BetTier.LOW, BetTier.MEDIUM, BetTier.HIGH]
        assert all(not r.has_bet and r.reason for r in results)


class TestSettlementGrading:
    @pytest.mark.parametrize(
        ("selection", "home", "away", "expected"),
        [
            ("home", 2, 1, Outcome.WIN),
            ("home", 1, 2, Outcome.LOSE),
            ("draw", 1, 1, Outcome.WIN),
            ("away", 0, 3, Outcome.WIN),
        ],
    )
    def test_match_winner(self, selection: str, home: int, away: int, expected: Outcome) -> None:
        assert grade_selection("1x2", selection, None, home, away) == expected

    def test_totals_push_on_an_exact_whole_line(self) -> None:
        """A total landing on the line returns the stake — neither win nor loss."""
        assert grade_selection("totals", "over", Decimal("3"), 2, 1) == Outcome.PUSH
        assert grade_selection("totals", "under", Decimal("3"), 2, 1) == Outcome.PUSH

    def test_totals_half_line_cannot_push(self) -> None:
        assert grade_selection("totals", "over", Decimal("2.5"), 2, 1) == Outcome.WIN
        assert grade_selection("totals", "under", Decimal("2.5"), 2, 1) == Outcome.LOSE

    def test_both_teams_to_score(self) -> None:
        assert grade_selection("btts", "yes", None, 1, 1) == Outcome.WIN
        assert grade_selection("btts", "yes", None, 3, 0) == Outcome.LOSE
        assert grade_selection("btts", "no", None, 3, 0) == Outcome.WIN

    def test_handicap_push_when_the_line_exactly_cancels_the_margin(self) -> None:
        assert grade_selection("handicap", "home", Decimal("-1"), 3, 2) == Outcome.PUSH

    def test_quarter_line_handicap_splits_the_stake(self) -> None:
        """A -0.25 line is half on 0 and half on -0.5: a one-goal win is a
        full win, a draw is a half loss."""
        assert grade_selection("handicap", "home", Decimal("-0.25"), 2, 1) == Outcome.WIN
        assert grade_selection("handicap", "home", Decimal("-0.25"), 1, 1) == Outcome.HALF_LOSE
        assert grade_selection("handicap", "home", Decimal("0.25"), 1, 1) == Outcome.HALF_WIN

    def test_correct_score(self) -> None:
        assert grade_selection("correct_score", "2-1", None, 2, 1) == Outcome.WIN
        assert grade_selection("correct_score", "2-1", None, 1, 1) == Outcome.LOSE

    def test_unknown_market_voids_rather_than_guessing(self) -> None:
        assert grade_selection("some_future_market", "x", None, 1, 0) == Outcome.VOID

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (Outcome.WIN, 2.0),
            (Outcome.LOSE, 0.0),
            (Outcome.PUSH, 1.0),
            (Outcome.VOID, 1.0),
            (Outcome.HALF_WIN, 1.5),
            (Outcome.HALF_LOSE, 0.5),
        ],
    )
    def test_payout_multipliers(self, outcome: Outcome, expected: float) -> None:
        assert payout_multiplier(outcome, 2.0) == pytest.approx(expected)


class TestMetrics:
    def test_no_bets_means_no_conclusion(self) -> None:
        interval = hit_rate([])
        assert interval.n == 0
        assert "no settled bets" in interval.describe()

    def test_small_samples_are_labelled_as_such(self) -> None:
        """A 55% hit rate over 20 bets must not read as evidence."""
        outcomes = [Outcome.WIN] * 11 + [Outcome.LOSE] * 9
        interval = hit_rate(outcomes)
        assert interval.n == 20
        assert not interval.is_meaningful
        assert "too few bets" in interval.describe()

    def test_interval_brackets_the_point_estimate(self) -> None:
        outcomes = [Outcome.WIN] * 60 + [Outcome.LOSE] * 40
        interval = hit_rate(outcomes)
        assert interval.low < interval.point < interval.high
        assert interval.point == pytest.approx(0.6, abs=0.01)
        assert interval.is_meaningful

    def test_pushes_are_excluded_from_the_rate(self) -> None:
        assert hit_rate([Outcome.WIN, Outcome.LOSE, Outcome.PUSH, Outcome.VOID]).n == 2

    def test_roi_of_break_even_bets_is_zero(self) -> None:
        stakes = [Decimal("10")] * 4
        payouts = [Decimal("20"), Decimal("0"), Decimal("20"), Decimal("0")]
        assert roi(stakes, payouts).point == pytest.approx(0.0)

    def test_brier_score_known_answers(self) -> None:
        assert brier_score([1.0, 0.0], [1, 0]) == pytest.approx(0.0)  # perfect
        assert brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)  # coin flip
        assert brier_score([0.0, 1.0], [1, 0]) == pytest.approx(1.0)  # perfectly wrong

    def test_log_loss_punishes_confident_errors(self) -> None:
        confident_wrong = log_loss([0.99], [0])
        hedged_wrong = log_loss([0.6], [0])
        assert confident_wrong > hedged_wrong

    def test_calibration_buckets_predictions_against_outcomes(self) -> None:
        """A well-calibrated model's 70% picks win about 70% of the time."""
        probabilities = [0.75] * 100
        outcomes = [1] * 75 + [0] * 25
        buckets = calibration(probabilities, outcomes)

        assert len(buckets) == 1
        assert buckets[0].predicted == pytest.approx(0.75)
        assert buckets[0].actual == pytest.approx(0.75)
        assert buckets[0].count == 100

    def test_calibration_exposes_an_overconfident_model(self) -> None:
        probabilities = [0.9] * 100
        outcomes = [1] * 50 + [0] * 50
        bucket = calibration(probabilities, outcomes)[0]
        assert bucket.predicted > bucket.actual  # says 90%, wins 50%

    def test_closing_line_value_signs(self) -> None:
        assert closing_line_value(2.2, 2.0) > 0  # beat the close
        assert closing_line_value(1.8, 2.0) < 0  # worse than the close
        assert closing_line_value(2.0, 2.0) == pytest.approx(0.0)

    def test_equity_curve_tracks_drawdown_and_streaks(self) -> None:
        stakes = [Decimal("10")] * 5
        payouts = [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("30"), Decimal("0")]
        curve = equity_curve(1000.0, stakes, payouts)

        assert curve.points[-1].bankroll == pytest.approx(1000 - 50 + 30)
        assert curve.longest_losing_streak == 3
        assert curve.max_drawdown > 0

    def test_outcome_mapping_excludes_pushes(self) -> None:
        assert outcome_to_binary(Outcome.WIN) == 1
        assert outcome_to_binary(Outcome.LOSE) == 0
        assert outcome_to_binary(Outcome.PUSH) is None
        assert outcome_to_binary(Outcome.VOID) is None

    def test_bootstrap_of_a_single_value_is_degenerate_not_wrong(self) -> None:
        interval = bootstrap_interval([0.5])
        assert interval.point == interval.low == interval.high == 0.5
        assert interval.n == 1
