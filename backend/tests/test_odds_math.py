"""Odds conversion, de-vigging, EV and Kelly.

Known-answer tests throughout: a fair coin at 2.0 must have exactly zero EV, a
de-vigged market must sum to 1, and full Kelly on a known edge has a closed
form. These are the calculations every downstream number depends on, so they
are pinned to arithmetic rather than to previous output.
"""

from decimal import Decimal

import pytest

from app.betting.odds import (
    DevigMethod,
    OddsFormat,
    american_to_decimal,
    best_price,
    decimal_to_american,
    decimal_to_fractional,
    devig,
    edge,
    expected_value,
    fair_odds,
    format_odds,
    fractional_to_decimal,
    implied_probability,
    overround,
)
from app.betting.staking import kelly_fraction, loss_frequency_phrase, recommend_stake


class TestConversions:
    @pytest.mark.parametrize(
        ("decimal_odds", "american"),
        [(2.0, 100), (1.5, -200), (3.0, 200), (1.91, -110)],
    )
    def test_decimal_american_round_trip(self, decimal_odds: float, american: int) -> None:
        assert decimal_to_american(decimal_odds) == american
        assert american_to_decimal(american) == pytest.approx(decimal_odds, abs=0.005)

    def test_fractional_conversions(self) -> None:
        assert decimal_to_fractional(3.0) == "2/1"
        assert decimal_to_fractional(1.5) == "1/2"
        assert fractional_to_decimal("2/1") == 3.0
        assert fractional_to_decimal("1/2") == 1.5

    def test_format_odds_per_display_setting(self) -> None:
        assert format_odds(2.5, OddsFormat.DECIMAL) == "2.50"
        assert format_odds(2.5, OddsFormat.AMERICAN) == "+150"
        assert format_odds(1.5, OddsFormat.AMERICAN) == "-200"
        assert format_odds(3.0, OddsFormat.FRACTIONAL) == "2/1"

    def test_implied_probability_of_evens_is_one_half(self) -> None:
        assert implied_probability(2.0) == 0.5

    def test_odds_at_or_below_one_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="must exceed 1.0"):
            implied_probability(1.0)


class TestDevigging:
    def test_fair_two_way_market_sums_to_one(self) -> None:
        """A market with no margin must come back unchanged."""
        fair = devig([2.0, 2.0])
        assert sum(fair) == pytest.approx(1.0)
        assert fair == pytest.approx([0.5, 0.5])

    @pytest.mark.parametrize(
        "method", [DevigMethod.POWER, DevigMethod.SHIN, DevigMethod.PROPORTIONAL]
    )
    def test_every_method_produces_a_probability_distribution(self, method: DevigMethod) -> None:
        fair = devig([1.91, 1.91], method=method)
        assert sum(fair) == pytest.approx(1.0, abs=1e-9)
        assert all(0 < p < 1 for p in fair)

    def test_three_way_market_sums_to_one(self) -> None:
        fair = devig([2.1, 3.6, 3.4])  # typical 1X2 board
        assert sum(fair) == pytest.approx(1.0, abs=1e-9)
        assert len(fair) == 3

    def test_overround_is_the_bookmaker_margin(self) -> None:
        assert overround([2.0, 2.0]) == pytest.approx(0.0)
        assert overround([1.91, 1.91]) == pytest.approx(0.0471, abs=1e-3)

    def test_power_method_shrinks_longshots_more_than_proportional(self) -> None:
        """The reason proportional de-vigging is not the default: it leaves
        longshots overstated, inventing edges that are not there."""
        prices = [1.2, 7.0, 15.0]  # heavy favourite, mid, longshot — 4.3% margin
        assert overround(prices) > 0, "the comparison only means anything with margin"

        power = devig(prices, method=DevigMethod.POWER)
        proportional = devig(prices, method=DevigMethod.PROPORTIONAL)

        assert power[2] < proportional[2], "longshot must be shrunk harder"
        assert power[0] > proportional[0], "favourite keeps more of its probability"

    def test_market_without_margin_is_simply_normalised(self) -> None:
        """Prices that already sum below 1 are an arbitrage, not a priced
        market — there is no margin to attribute, so all methods agree."""
        prices = [1.5, 8.0]
        assert overround(prices) < 0
        assert devig(prices, method=DevigMethod.POWER) == pytest.approx(
            devig(prices, method=DevigMethod.PROPORTIONAL)
        )

    def test_symmetric_market_is_unaffected_by_method_choice(self) -> None:
        """With equal prices there is no favourite-longshot bias to correct."""
        power = devig([1.91, 1.91], method=DevigMethod.POWER)
        proportional = devig([1.91, 1.91], method=DevigMethod.PROPORTIONAL)
        assert power == pytest.approx(proportional, abs=1e-6)

    def test_empty_market_is_empty(self) -> None:
        assert devig([]) == []


class TestExpectedValue:
    def test_fair_coin_at_evens_has_zero_expected_value(self) -> None:
        """The canonical known-answer case."""
        assert expected_value(0.5, 2.0) == pytest.approx(0.0)

    def test_positive_edge_gives_positive_expected_value(self) -> None:
        # 55% at evens: 0.55*1 - 0.45 = +0.10 per unit.
        assert expected_value(0.55, 2.0) == pytest.approx(0.10)

    def test_negative_edge_gives_negative_expected_value(self) -> None:
        assert expected_value(0.45, 2.0) == pytest.approx(-0.10)

    def test_expected_value_scales_with_stake(self) -> None:
        assert expected_value(0.55, 2.0, stake=100) == pytest.approx(10.0)

    def test_edge_is_model_minus_market(self) -> None:
        assert edge(0.55, 0.50) == pytest.approx(0.05)

    def test_fair_odds_invert_probability(self) -> None:
        assert fair_odds(0.5) == 2.0
        assert fair_odds(0.25) == 4.0


class TestLineShopping:
    def test_best_price_is_the_highest_decimal_odds(self) -> None:
        book, price = best_price({"bet365": 2.10, "pinnacle": 2.24, "william_hill": 2.05})
        assert (book, price) == ("pinnacle", 2.24)

    def test_no_prices_raises(self) -> None:
        with pytest.raises(ValueError, match="no prices"):
            best_price({})


class TestKelly:
    def test_no_edge_means_no_bet(self) -> None:
        assert kelly_fraction(0.5, 2.0) == 0.0

    def test_negative_edge_means_no_bet(self) -> None:
        assert kelly_fraction(0.4, 2.0) == 0.0

    def test_known_closed_form(self) -> None:
        # f* = (bp - q)/b with b=1, p=0.55, q=0.45 -> 0.10
        assert kelly_fraction(0.55, 2.0) == pytest.approx(0.10)
        # b=2, p=0.4, q=0.6 -> (0.8-0.6)/2 = 0.10
        assert kelly_fraction(0.40, 3.0) == pytest.approx(0.10)

    def test_certainty_stakes_everything(self) -> None:
        assert kelly_fraction(1.0, 2.0) == pytest.approx(1.0)

    def test_quarter_kelly_is_a_quarter_of_full(self) -> None:
        bankroll = Decimal("1000")
        rec = recommend_stake(0.55, 2.0, bankroll, kelly_multiplier=0.25, max_fraction=1.0)
        assert rec.full_kelly_fraction == pytest.approx(0.10)
        assert rec.kelly_fraction == pytest.approx(0.025)
        assert rec.stake == Decimal("25.00")

    def test_stake_cap_binds_before_kelly(self) -> None:
        rec = recommend_stake(0.90, 3.0, Decimal("1000"), kelly_multiplier=0.5, max_fraction=0.02)
        assert rec.capped_by == "tier_cap"
        assert rec.stake == Decimal("20.00")

    def test_exposure_cap_binds_last(self) -> None:
        rec = recommend_stake(
            0.55,
            2.0,
            Decimal("1000"),
            kelly_multiplier=0.25,
            max_fraction=1.0,
            remaining_exposure=Decimal("10"),
        )
        assert rec.capped_by == "exposure_cap"
        assert rec.stake == Decimal("10.00")

    def test_no_edge_recommends_no_stake(self) -> None:
        rec = recommend_stake(0.45, 2.0, Decimal("1000"))
        assert rec.is_zero
        assert rec.expected_value < 0

    def test_tiny_stake_becomes_no_bet(self) -> None:
        """Below the minimum, the honest answer is no bet, not a token one."""
        rec = recommend_stake(0.51, 2.0, Decimal("10"), kelly_multiplier=0.25)
        assert rec.is_zero


class TestPlainLanguage:
    def test_loss_frequency_is_stated_so_the_downside_registers(self) -> None:
        assert "4 times in 10" in loss_frequency_phrase(0.6)
        assert "7 times in 10" in loss_frequency_phrase(0.3)

    def test_very_likely_bets_use_a_finer_scale(self) -> None:
        assert "in 100" in loss_frequency_phrase(0.97)
