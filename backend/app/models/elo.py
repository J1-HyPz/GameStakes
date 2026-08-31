"""Club Elo ratings — a secondary signal ensembled with the scoring model.

Elo is crude but robust: it needs only results, updates online, and degrades
gracefully when a team has little history. That makes it a useful counterweight
to a Poisson model fitted on goals, which is sharper but noisier over small
samples.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_RATING = 1500.0
# Home advantage in rating points. Fitted per league in `fit_home_advantage`;
# this is the fallback for a league with no history yet.
DEFAULT_HOME_ADVANTAGE = 60.0


@dataclass
class EloConfig:
    k_factor: float = 20.0
    home_advantage: float = DEFAULT_HOME_ADVANTAGE
    # Goal difference amplifies the update: a 4-0 win is stronger evidence
    # than 1-0, but with diminishing returns so blowouts do not dominate.
    margin_scaling: bool = True
    # Pull ratings toward the mean between seasons; squads and form reset.
    season_regression: float = 0.25


@dataclass
class EloModel:
    config: EloConfig = field(default_factory=EloConfig)
    ratings: dict[int, float] = field(default_factory=dict)

    def rating(self, team_id: int) -> float:
        return self.ratings.get(team_id, DEFAULT_RATING)

    def expected_home_score(self, home_id: int, away_id: int) -> float:
        """Expected result for the home team on a 0-1 scale (0.5 = even)."""
        diff = self.rating(home_id) + self.config.home_advantage - self.rating(away_id)
        return 1.0 / (1.0 + 10 ** (-diff / 400.0))

    def win_probabilities(
        self, home_id: int, away_id: int, draw_share: float = 0.26
    ) -> tuple[float, float, float]:
        """Split the expected score into home/draw/away.

        Elo yields an expected score, not three outcome probabilities; the draw
        share has to come from somewhere. `draw_share` is the league's observed
        draw rate, and the remainder is split in proportion to the Elo
        expectation. Football-specific and deliberately simple — the scoring
        model is what produces sharp 1X2 numbers.
        """
        expected = self.expected_home_score(home_id, away_id)
        remaining = 1.0 - draw_share
        home = remaining * expected
        away = remaining * (1.0 - expected)
        return home, draw_share, away

    def update(
        self, home_id: int, away_id: int, home_score: int, away_score: int
    ) -> tuple[float, float]:
        """Apply one result; returns the new (home, away) ratings."""
        expected = self.expected_home_score(home_id, away_id)
        if home_score > away_score:
            actual = 1.0
        elif home_score < away_score:
            actual = 0.0
        else:
            actual = 0.5

        k = self.config.k_factor
        if self.config.margin_scaling:
            k *= _margin_multiplier(abs(home_score - away_score))

        change = k * (actual - expected)
        home_rating = self.rating(home_id) + change
        away_rating = self.rating(away_id) - change
        self.ratings[home_id] = home_rating
        self.ratings[away_id] = away_rating
        return home_rating, away_rating

    def regress_to_mean(self) -> None:
        """Between seasons, pull every rating toward the league average."""
        factor = self.config.season_regression
        for team_id, rating in self.ratings.items():
            self.ratings[team_id] = rating + factor * (DEFAULT_RATING - rating)


def _margin_multiplier(goal_difference: int) -> float:
    """Diminishing weight for larger winning margins (FiveThirtyEight-style)."""
    if goal_difference <= 1:
        return 1.0
    return 1.0 + 0.5 * (goal_difference - 1) / goal_difference * 2
