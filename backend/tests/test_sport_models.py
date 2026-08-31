"""Basketball, American football and combat sport models."""

from datetime import date, timedelta

import pytest

from app.db.enums import Confidence, VictoryMethod
from app.models import american_football as nfl
from app.models import basketball as nba
from app.models import combat, football
from app.models.elo import EloModel

SEASON_START = date(2026, 1, 1)
AS_OF = date(2026, 4, 1)


class TestFootballModel:
    def test_fit_needs_no_history_to_return_usable_priors(self) -> None:
        params = football.fit([], as_of=AS_OF)
        assert params.home_advantage == football.DEFAULT_HOME_ADVANTAGE
        assert params.attack == {}

    def test_stronger_attack_produces_more_expected_goals(self) -> None:
        # Team 1 wins heavily and often; team 2 loses.
        matches = [
            football.Match(1, 2, 3, 0, SEASON_START + timedelta(days=i * 7)) for i in range(12)
        ] + [
            football.Match(2, 1, 0, 2, SEASON_START + timedelta(days=i * 7 + 3)) for i in range(12)
        ]
        params = football.fit(matches, as_of=AS_OF)

        strong = football.project(params, 1, 2)
        weak = football.project(params, 2, 1)
        assert strong.home_lambda > weak.home_lambda

    def test_recency_weighting_favours_recent_form(self) -> None:
        old = [football.Match(1, 2, 4, 0, date(2025, 1, 10) + timedelta(days=i)) for i in range(10)]
        recent = [football.Match(1, 2, 0, 3, AS_OF - timedelta(days=i + 1)) for i in range(10)]

        weights = football.decay_weights(
            [m.played_on for m in old + recent], AS_OF, half_life_days=60
        )
        assert weights[-1] > weights[0] * 5, "recent matches must dominate"

    def test_confidence_reflects_sample_size(self) -> None:
        assert football.assess_confidence(20, 20)[0] == Confidence.HIGH
        assert football.assess_confidence(8, 10)[0] == Confidence.MEDIUM
        assert football.assess_confidence(2, 20)[0] == Confidence.LOW

    def test_absences_reduce_expected_goals(self) -> None:
        matches = [
            football.Match(1, 2, 2, 1, SEASON_START + timedelta(days=i * 5)) for i in range(20)
        ]
        params = football.fit(matches, as_of=AS_OF)

        full = football.project(params, 1, 2)
        depleted = football.project(params, 1, 2, home_absences=0.5)
        assert depleted.home_lambda < full.home_lambda

    def test_elo_blend_stays_a_probability_distribution(self) -> None:
        blended = football.blend_with_elo((0.5, 0.3, 0.2), (0.4, 0.3, 0.3), elo_weight=0.3)
        assert sum(blended) == pytest.approx(1.0)
        assert all(p > 0 for p in blended)


class TestElo:
    def test_winning_raises_rating_and_losing_lowers_it(self) -> None:
        elo = EloModel()
        home, away = elo.update(1, 2, 2, 0)
        assert home > 1500 and away < 1500

    def test_home_advantage_raises_expected_score(self) -> None:
        elo = EloModel()
        assert elo.expected_home_score(1, 2) > 0.5  # equal ratings, home edge

    def test_bigger_margins_move_ratings_further(self) -> None:
        narrow, blowout = EloModel(), EloModel()
        narrow.update(1, 2, 1, 0)
        blowout.update(1, 2, 5, 0)
        assert blowout.rating(1) > narrow.rating(1)

    def test_season_regression_pulls_toward_the_mean(self) -> None:
        elo = EloModel()
        elo.ratings[1] = 1800.0
        elo.regress_to_mean()
        assert 1500 < elo.ratings[1] < 1800


class TestBasketballModel:
    def _games(self, n: int = 40) -> list[nba.Game]:
        return [
            nba.Game(1, 2, 112, 104, SEASON_START + timedelta(days=i * 2), possessions=100.0)
            for i in range(n)
        ]

    def test_thin_history_falls_back_to_league_priors(self) -> None:
        params = nba.fit([], as_of=AS_OF, league="nba")
        assert params.margin_sd == nba.LEAGUE_PRIORS["nba"]["margin_sd"]

    def test_margin_sd_is_fitted_not_hardcoded(self) -> None:
        """WNBA and NCAA differ from the NBA; borrowing its number misprices
        every spread in those leagues."""
        volatile = [
            nba.Game(
                1, 2, 120 + (i % 5) * 10, 90, SEASON_START + timedelta(days=i), possessions=100.0
            )
            for i in range(40)
        ]
        steady = [
            nba.Game(1, 2, 110, 108, SEASON_START + timedelta(days=i), possessions=100.0)
            for i in range(40)
        ]
        assert nba.fit(volatile, AS_OF).margin_sd > nba.fit(steady, AS_OF).margin_sd

    def test_pace_drives_the_total(self) -> None:
        fast = [
            nba.Game(1, 2, 120, 118, SEASON_START + timedelta(days=i), possessions=105.0)
            for i in range(40)
        ]
        slow = [
            nba.Game(1, 2, 95, 93, SEASON_START + timedelta(days=i), possessions=90.0)
            for i in range(40)
        ]
        fast_total = nba.project(nba.fit(fast, AS_OF), 1, 2).expected_total
        slow_total = nba.project(nba.fit(slow, AS_OF), 1, 2).expected_total
        assert fast_total > slow_total

    def test_back_to_back_costs_points(self) -> None:
        params = nba.fit(self._games(), AS_OF)
        rested = nba.project(params, 1, 2)
        tired = nba.project(params, 1, 2, home_back_to_back=True)
        assert tired.home_points < rested.home_points

    def test_altitude_penalises_the_visiting_side(self) -> None:
        params = nba.fit(self._games(), AS_OF)
        sea_level = nba.project(params, 1, 2)
        denver = nba.project(params, 1, 2, altitude_m=1600)
        assert denver.away_points < sea_level.away_points


class TestAmericanFootballModel:
    def _games(self, n: int = 32) -> list[nfl.Game]:
        return [nfl.Game(1, 2, 24, 20, SEASON_START + timedelta(days=i * 7)) for i in range(n)]

    def test_league_priors_differ_between_nfl_and_college(self) -> None:
        """College margins are much wider — talent gaps are larger."""
        assert nfl.LEAGUE_PRIORS["ncaa-fbs"]["margin_sd"] > nfl.LEAGUE_PRIORS["nfl"]["margin_sd"]

    def test_nfl_key_numbers_are_three_and_seven(self) -> None:
        params = nfl.fit([], AS_OF, league="nfl")
        assert 3 in params.key_numbers and 7 in params.key_numbers
        assert params.key_numbers[3] > params.key_numbers[7], "3 is the most common margin"

    def test_college_uses_its_own_key_numbers(self) -> None:
        college = nfl.fit([], AS_OF, league="ncaa-fbs")
        assert college.key_numbers == nfl.NCAA_KEY_NUMBERS

    def test_wind_suppresses_the_total(self) -> None:
        params = nfl.fit(self._games(), AS_OF)
        calm = nfl.project(params, 1, 2, wind_mph=2)
        windy = nfl.project(params, 1, 2, wind_mph=25)
        assert windy.expected_total < calm.expected_total

    def test_domes_ignore_weather(self) -> None:
        params = nfl.fit(self._games(), AS_OF)
        outdoor = nfl.project(params, 1, 2, wind_mph=25)
        indoor = nfl.project(params, 1, 2, wind_mph=25, is_dome=True)
        assert indoor.expected_total > outdoor.expected_total

    def test_light_wind_has_no_effect(self) -> None:
        assert nfl._wind_penalty(8) == 0.0
        assert nfl._wind_penalty(20) > 0

    def test_short_week_costs_points(self) -> None:
        params = nfl.fit(self._games(), AS_OF)
        normal = nfl.project(params, 1, 2, home_rest_days=7)
        thursday = nfl.project(params, 1, 2, home_rest_days=4)
        assert thursday.expected_margin < normal.expected_margin


class TestCombatModel:
    def _career(self, fighter: int, opponent_start: int, wins: int) -> list[combat.Bout]:
        return [
            combat.Bout(
                winner_id=fighter,
                fighter_a_id=fighter,
                fighter_b_id=opponent_start + i,
                fought_on=SEASON_START + timedelta(days=i * 120),
                method=VictoryMethod.KO_TKO,
            )
            for i in range(wins)
        ]

    def test_unknown_fighters_are_close_to_a_coin_flip(self) -> None:
        """With no data the honest answer is 'we don't know'."""
        params = combat.fit([], AS_OF)
        projection = combat.project(params, 1, 2)
        assert projection.win_probability_a == pytest.approx(0.49, abs=0.05)

    def test_winning_record_raises_win_probability(self) -> None:
        bouts = self._career(1, 100, 8)
        params = combat.fit(bouts, AS_OF)
        projection = combat.project(params, 1, 999)  # 999 is unrated
        assert projection.win_probability_a > 0.5

    def test_inactivity_widens_uncertainty(self) -> None:
        """A fighter who last competed two years ago is not as well understood
        as one who fought last month."""
        bouts = self._career(1, 100, 6)
        recent = combat.fit(bouts, as_of=bouts[-1].fought_on + timedelta(days=30))
        stale = combat.fit(bouts, as_of=bouts[-1].fought_on + timedelta(days=730))
        assert stale.rating_for(1).deviation > recent.rating_for(1).deviation

    def test_confidence_never_reaches_high(self) -> None:
        """Deliberate cap: the data never supports staking low-risk money on a
        fight."""
        bouts = self._career(1, 100, 15) + self._career(2, 200, 15)
        params = combat.fit(bouts, AS_OF)
        projection = combat.project(params, 1, 2)
        assert projection.confidence in {Confidence.LOW, Confidence.MEDIUM}

    def test_projection_carries_an_explicit_caveat(self) -> None:
        projection = combat.project(combat.fit([], AS_OF), 1, 2)
        assert "widest uncertainty" in projection.caveat

    def test_outcome_probabilities_sum_to_one(self) -> None:
        projection = combat.project(combat.fit([], AS_OF), 1, 2)
        total = (
            projection.win_probability_a
            + projection.win_probability_b
            + projection.draw_probability
        )
        assert total == pytest.approx(1.0, abs=1e-6)

    def test_method_split_matches_the_win_probability(self) -> None:
        projection = combat.project(combat.fit([], AS_OF), 1, 2)
        method_total = (
            projection.method_a.ko_tko
            + projection.method_a.submission
            + projection.method_a.decision
        )
        assert method_total == pytest.approx(projection.win_probability_a, abs=1e-6)

    def test_round_distribution_covers_the_scheduled_rounds(self) -> None:
        projection = combat.project(combat.fit([], AS_OF), 1, 2, scheduled_rounds=5)
        assert len(projection.round_finish_probabilities) == 5
        # Earlier rounds carry more hazard: fighters are fresh and take risks.
        assert projection.round_finish_probabilities[0] > projection.round_finish_probabilities[-1]

    def test_finish_and_distance_probabilities_are_complementary(self) -> None:
        projection = combat.project(combat.fit([], AS_OF), 1, 2)
        assert sum(projection.round_finish_probabilities) + projection.distance_probability == (
            pytest.approx(1.0, abs=1e-6)
        )

    def test_short_notice_replacement_lowers_win_probability(self) -> None:
        params = combat.fit(self._career(1, 100, 8), AS_OF)
        normal = combat.project(params, 1, 2)
        short = combat.project(params, 1, 2, a_short_notice=True)
        assert short.win_probability_a < normal.win_probability_a

    def test_simulation_produces_aligned_draws(self) -> None:
        projection = combat.project(combat.fit([], AS_OF), 1, 2, scheduled_rounds=3)
        draws = combat.simulate_bout(projection, n_iterations=5_000, seed=42)

        assert len(draws["winner"]) == 5_000
        assert set(draws["winner"].tolist()) <= {0, 1, 2}
        assert draws["end_round"].max() <= 3
        # A bout that went the distance has no finishing round.
        assert ((draws["end_round"] == 0) == (draws["went_distance"] == 1)).all()

    def test_simulation_is_reproducible(self) -> None:
        projection = combat.project(combat.fit([], AS_OF), 1, 2)
        a = combat.simulate_bout(projection, 1_000, seed=7)
        b = combat.simulate_bout(projection, 1_000, seed=7)
        assert (a["winner"] == b["winner"]).all()
