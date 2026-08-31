"""Simulation engine, market derivation and correlated parlay pricing.

The correlation tests are the important ones: they pin the property that makes
the bet builder honest — same-game legs priced from the joint distribution, not
from the product of marginals.
"""

import numpy as np
import pytest

from app.betting.parlay import (
    Leg,
    combined_odds,
    duplicate_selection,
    is_contradictory,
    price_parlay,
)
from app.sim.engine import (
    ScoreSimulator,
    SimulationResult,
    joint_probability,
    seed_for,
)
from app.sim.markets import (
    both_teams_to_score,
    correct_score,
    match_winner,
    score_heatmap,
    totals,
)


@pytest.fixture
def sim() -> SimulationResult:
    return ScoreSimulator(n_iterations=20_000).simulate_poisson(
        fixture_id=1, model_version_id=1, home_lambda=1.7, away_lambda=1.1, rho=-0.05
    )


class TestReproducibility:
    def test_same_seed_gives_identical_draws(self) -> None:
        """The reproducibility contract: same inputs and seed, same prediction."""
        a = ScoreSimulator(2_000).simulate_poisson(1, 1, 1.5, 1.2, seed=42)
        b = ScoreSimulator(2_000).simulate_poisson(1, 1, 1.5, 1.2, seed=42)
        assert np.array_equal(a.draws["home_score"], b.draws["home_score"])
        assert np.array_equal(a.draws["away_score"], b.draws["away_score"])

    def test_different_seeds_give_different_draws(self) -> None:
        a = ScoreSimulator(2_000).simulate_poisson(1, 1, 1.5, 1.2, seed=1)
        b = ScoreSimulator(2_000).simulate_poisson(1, 1, 1.5, 1.2, seed=2)
        assert not np.array_equal(a.draws["home_score"], b.draws["home_score"])

    def test_seed_is_derived_deterministically_from_the_fixture(self) -> None:
        assert seed_for(123, 4) == seed_for(123, 4)
        assert seed_for(123, 4) != seed_for(124, 4)

    def test_default_seed_is_used_and_recorded(self) -> None:
        result = ScoreSimulator(1_000).simulate_poisson(7, 3, 1.4, 1.1)
        assert result.seed == seed_for(7, 3)

    def test_iteration_count_is_validated(self) -> None:
        with pytest.raises(ValueError, match="n_iterations"):
            ScoreSimulator(0)
        with pytest.raises(ValueError, match="n_iterations"):
            ScoreSimulator(1_000_000)


class TestScoreSimulation:
    def test_means_track_the_expected_goals(self, sim: SimulationResult) -> None:
        assert float(np.mean(sim.draws["home_score"])) == pytest.approx(1.7, abs=0.08)
        assert float(np.mean(sim.draws["away_score"])) == pytest.approx(1.1, abs=0.08)

    def test_stronger_side_wins_more_often(self, sim: SimulationResult) -> None:
        outcomes = {o.selection: o.probability for o in match_winner(sim)}
        assert outcomes["home"] > outcomes["away"]
        assert sum(outcomes.values()) == pytest.approx(1.0, abs=1e-9)

    def test_summary_reports_a_distribution_not_a_point(self, sim: SimulationResult) -> None:
        summary = sim.summary()
        assert summary["home_score"]["sd"] > 0
        assert summary["home_score"]["p10"] <= summary["home_score"]["p50"]
        assert summary["home_score"]["p50"] <= summary["home_score"]["p90"]

    def test_dixon_coles_lifts_low_score_cells(self) -> None:
        """The correction exists because independent Poisson misprices 0-0,
        1-0, 0-1 and 1-1 — a large share of football outcomes."""
        plain = ScoreSimulator(40_000).simulate_poisson(1, 1, 1.3, 1.1, rho=0.0, seed=7)
        adjusted = ScoreSimulator(40_000).simulate_poisson(1, 1, 1.3, 1.1, rho=-0.15, seed=7)

        def draw_share(result: SimulationResult) -> float:
            home, away = result.draws["home_score"], result.draws["away_score"]
            return float(np.mean((home == away) & (home <= 1)))

        assert draw_share(adjusted) > draw_share(plain)

    def test_margin_simulation_keeps_scores_consistent(self) -> None:
        result = ScoreSimulator(5_000).simulate_margin_total(
            1, 1, expected_margin=3.0, expected_total=45.0, margin_sd=13.5, total_sd=10.0, seed=3
        )
        home, away = result.draws["home_score"], result.draws["away_score"]
        assert np.array_equal(result.draws["margin"], home - away)
        assert np.array_equal(result.draws["total"], home + away)
        assert (home >= 0).all() and (away >= 0).all()

    def test_key_numbers_get_extra_mass(self) -> None:
        """3 and 7 are far more common NFL margins than their neighbours."""
        plain = ScoreSimulator(20_000).simulate_margin_total(1, 1, 0.0, 44.0, 13.5, 10.0, seed=11)
        keyed = ScoreSimulator(20_000).simulate_margin_total(
            1, 1, 0.0, 44.0, 13.5, 10.0, seed=11, key_numbers={3: 0.5, 7: 0.4}
        )
        for key in (3, 7):
            plain_share = float(np.mean(np.abs(plain.draws["margin"]) == key))
            keyed_share = float(np.mean(np.abs(keyed.draws["margin"]) == key))
            assert keyed_share > plain_share


class TestMarkets:
    def test_markets_partition_the_outcome_space(self, sim: SimulationResult) -> None:
        assert sum(o.probability for o in match_winner(sim)) == pytest.approx(1.0)
        assert sum(o.probability for o in both_teams_to_score(sim)) == pytest.approx(1.0)

    def test_over_and_under_a_half_line_are_complements(self, sim: SimulationResult) -> None:
        outcomes = {o.selection: o.probability for o in totals(sim, [2.5])}
        assert outcomes["over"] + outcomes["under"] == pytest.approx(1.0)

    def test_whole_number_line_leaves_room_for_a_push(self, sim: SimulationResult) -> None:
        outcomes = {o.selection: o.probability for o in totals(sim, [3.0])}
        assert outcomes["over"] + outcomes["under"] < 1.0

    def test_correct_score_probabilities_sum_to_one(self, sim: SimulationResult) -> None:
        assert sum(o.probability for o in correct_score(sim)) == pytest.approx(1.0)

    def test_fair_price_inverts_probability(self, sim: SimulationResult) -> None:
        home = next(o for o in match_winner(sim) if o.selection == "home")
        assert home.fair_price == pytest.approx(1 / home.probability)

    def test_heatmap_is_a_probability_grid(self, sim: SimulationResult) -> None:
        grid = score_heatmap(sim, max_goals=5)
        assert len(grid) == 6 and len(grid[0]) == 6
        assert 0 < sum(sum(row) for row in grid) <= 1.0


def _leg(
    sim: SimulationResult, mask: np.ndarray, odds: float, fixture_id: int = 1, market: str = "m"
) -> Leg:
    return Leg(
        fixture_id=fixture_id,
        market=market,
        selection="s",
        line=None,
        decimal_odds=odds,
        model_probability=float(np.mean(mask)),
        devigged_probability=None,
        bookmaker="test",
        mask=mask,
    )


class TestCorrelation:
    def test_same_game_legs_are_priced_from_the_joint_distribution(
        self, sim: SimulationResult
    ) -> None:
        """Aligned same-game legs are likelier together than independence
        implies; multiplying marginals would underprice the parlay."""
        home, away = sim.draws["home_score"], sim.draws["away_score"]
        home_win = _leg(sim, home > away, 2.0)
        over_two_five = _leg(sim, (home + away) > 2.5, 1.9, market="totals")

        price = price_parlay([home_win, over_two_five])

        assert price.combined_probability != pytest.approx(price.naive_probability, abs=1e-4)
        assert price.correlation_effect == pytest.approx(
            price.combined_probability - price.naive_probability
        )

    def test_perfectly_correlated_legs_price_as_a_single_leg(self, sim: SimulationResult) -> None:
        """A leg parlayed with itself cannot make the outcome less likely."""
        home, away = sim.draws["home_score"], sim.draws["away_score"]
        mask = home > away
        first = _leg(sim, mask, 2.0)
        second = _leg(sim, mask.copy(), 2.0, market="other")

        price = price_parlay([first, second])

        assert price.combined_probability == pytest.approx(float(np.mean(mask)))
        assert price.combined_probability > price.naive_probability

    def test_mutually_exclusive_legs_are_flagged_not_priced(self, sim: SimulationResult) -> None:
        home, away = sim.draws["home_score"], sim.draws["away_score"]
        legs = [_leg(sim, home > away, 2.0), _leg(sim, away > home, 3.5, market="other")]

        assert is_contradictory(legs)
        assert price_parlay(legs).combined_probability == 0.0

    def test_cross_game_legs_multiply(self, sim: SimulationResult) -> None:
        """Different fixtures are independent, so the product is correct there."""
        other = ScoreSimulator(20_000).simulate_poisson(2, 1, 1.4, 1.2, seed=9)
        first = _leg(sim, sim.draws["home_score"] > sim.draws["away_score"], 2.0, fixture_id=1)
        second = _leg(
            other, other.draws["home_score"] > other.draws["away_score"], 2.2, fixture_id=2
        )

        price = price_parlay([first, second])

        assert price.combined_probability == pytest.approx(
            first.model_probability * second.model_probability
        )
        assert price.correlation_effect == pytest.approx(0.0, abs=1e-9)

    def test_combined_odds_multiply(self, sim: SimulationResult) -> None:
        legs = [
            _leg(sim, sim.draws["home_score"] > 0, 2.0),
            _leg(sim, sim.draws["away_score"] > 0, 1.8, market="x"),
        ]
        assert combined_odds(legs) == pytest.approx(3.6)

    def test_duplicate_selection_is_detected(self, sim: SimulationResult) -> None:
        mask = sim.draws["home_score"] > 0
        legs = [_leg(sim, mask, 2.0), _leg(sim, mask.copy(), 2.0)]
        assert duplicate_selection(legs)

    def test_joint_probability_helper_reports_both_figures(self, sim: SimulationResult) -> None:
        home, away = sim.draws["home_score"], sim.draws["away_score"]
        joint, naive = joint_probability(sim, [home > away, (home + away) > 2.5])
        assert 0 <= joint <= 1
        assert joint != pytest.approx(naive, abs=1e-4)


class TestPersistence:
    def test_draws_round_trip_through_disk(self, sim: SimulationResult, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = sim.save(tmp_path)
        loaded = SimulationResult.load_draws(path)
        assert np.array_equal(loaded["home_score"], sim.draws["home_score"])

    def test_empty_parlay_is_priced_as_nothing(self) -> None:
        price = price_parlay([])
        assert price.combined_probability == 0.0
        assert price.combined_odds == 1.0
