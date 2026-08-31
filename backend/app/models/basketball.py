"""Basketball model: possession-based ratings.

Points are pace times efficiency, so the model projects those separately —
a fast team and an efficient team produce very different totals from the same
nominal "strength". The Four Factors (shooting, turnovers, rebounding, free
throws) are carried as features for later refinement.

Margin standard deviation is **fitted from data, not hardcoded**. The NBA sits
near 11-12 points, but the WNBA and NCAA differ enough that borrowing the NBA
number would misprice every spread in those leagues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

from app.core.logging import get_logger
from app.db.enums import Confidence

log = get_logger(__name__)

MODEL_NAME = "basketball-pace-efficiency"
MODEL_VERSION = "1.0.0"

# Fallbacks when a league has too little history to fit. Deliberately explicit:
# these are priors to be replaced, not constants to rely on.
LEAGUE_PRIORS = {
    "nba": {"pace": 99.0, "rating": 113.0, "margin_sd": 11.5, "home_advantage": 2.5},
    "wnba": {"pace": 96.0, "rating": 102.0, "margin_sd": 10.5, "home_advantage": 2.2},
    "ncaa-mbb": {"pace": 68.0, "rating": 105.0, "margin_sd": 11.0, "home_advantage": 3.2},
    "ncaa-wbb": {"pace": 70.0, "rating": 95.0, "margin_sd": 12.0, "home_advantage": 3.0},
    "euroleague": {"pace": 72.0, "rating": 110.0, "margin_sd": 10.0, "home_advantage": 2.8},
}
DEFAULT_PRIOR = {"pace": 95.0, "rating": 108.0, "margin_sd": 11.5, "home_advantage": 2.5}


@dataclass(frozen=True)
class Game:
    home_id: int
    away_id: int
    home_points: int
    away_points: int
    played_on: date
    possessions: float | None = None


@dataclass
class BasketballParameters:
    offensive_rating: dict[int, float] = field(default_factory=dict)
    defensive_rating: dict[int, float] = field(default_factory=dict)
    pace: dict[int, float] = field(default_factory=dict)
    league_pace: float = DEFAULT_PRIOR["pace"]
    league_rating: float = DEFAULT_PRIOR["rating"]
    home_advantage: float = DEFAULT_PRIOR["home_advantage"]
    margin_sd: float = DEFAULT_PRIOR["margin_sd"]
    total_sd: float = 13.0
    games_per_team: dict[int, int] = field(default_factory=dict)

    def as_hyperparameters(self) -> dict[str, float | int]:
        return {
            "league_pace": self.league_pace,
            "league_rating": self.league_rating,
            "home_advantage": self.home_advantage,
            "margin_sd": self.margin_sd,
            "total_sd": self.total_sd,
            "n_teams": len(self.offensive_rating),
        }


@dataclass(frozen=True)
class BasketballProjection:
    expected_margin: float
    expected_total: float
    margin_sd: float
    total_sd: float
    home_points: float
    away_points: float
    confidence: Confidence
    confidence_score: float
    detail: dict[str, float]


def fit(
    games: list[Game], as_of: date, league: str = "nba", half_life_days: float = 45.0
) -> BasketballParameters:
    """Fit per-team pace and efficiency, and the league's margin spread.

    Basketball's half-life is much shorter than football's — rotations, injuries
    and trades change a team inside a season, and a 45-day half-life keeps the
    ratings responsive without chasing single games.
    """
    prior = LEAGUE_PRIORS.get(league, DEFAULT_PRIOR)
    if len(games) < 20:
        return BasketballParameters(
            league_pace=prior["pace"],
            league_rating=prior["rating"],
            home_advantage=prior["home_advantage"],
            margin_sd=prior["margin_sd"],
        )

    weights = _decay_weights([g.played_on for g in games], as_of, half_life_days)
    league_pace = float(
        np.average([g.possessions or prior["pace"] for g in games], weights=weights)
    )
    all_points = np.array([g.home_points for g in games] + [g.away_points for g in games])
    doubled = np.concatenate([weights, weights])
    league_rating = float(np.average(all_points, weights=doubled)) / league_pace * 100

    offensive: dict[int, list[tuple[float, float]]] = {}
    defensive: dict[int, list[tuple[float, float]]] = {}
    paces: dict[int, list[tuple[float, float]]] = {}
    counts: dict[int, int] = {}

    for game, weight in zip(games, weights, strict=True):
        possessions = game.possessions or league_pace
        for team, points, against in (
            (game.home_id, game.home_points, game.away_points),
            (game.away_id, game.away_points, game.home_points),
        ):
            offensive.setdefault(team, []).append((points / possessions * 100, weight))
            defensive.setdefault(team, []).append((against / possessions * 100, weight))
            paces.setdefault(team, []).append((possessions, weight))
            counts[team] = counts.get(team, 0) + 1

    # Margin spread fitted from residuals against a simple rating difference.
    margins = np.array([g.home_points - g.away_points for g in games], dtype=float)
    home_advantage = float(np.average(margins, weights=weights))
    margin_sd = float(np.sqrt(np.average((margins - home_advantage) ** 2, weights=weights)))
    totals = np.array([g.home_points + g.away_points for g in games], dtype=float)
    total_sd = float(np.sqrt(np.average((totals - totals.mean()) ** 2, weights=weights)))

    return BasketballParameters(
        offensive_rating={t: _weighted_mean(v) for t, v in offensive.items()},
        defensive_rating={t: _weighted_mean(v) for t, v in defensive.items()},
        pace={t: _weighted_mean(v) for t, v in paces.items()},
        league_pace=league_pace,
        league_rating=league_rating,
        home_advantage=home_advantage,
        margin_sd=margin_sd,
        total_sd=total_sd,
        games_per_team=counts,
    )


def project(
    params: BasketballParameters,
    home_id: int,
    away_id: int,
    *,
    home_rest_days: int | None = None,
    away_rest_days: int | None = None,
    home_back_to_back: bool = False,
    away_back_to_back: bool = False,
    altitude_m: int = 0,
    neutral_venue: bool = False,
) -> BasketballProjection:
    """Project points from pace and efficiency, with schedule adjustments."""
    home_shrink = _shrinkage(params.games_per_team.get(home_id, 0))
    away_shrink = _shrinkage(params.games_per_team.get(away_id, 0))

    home_off = _blend(params.offensive_rating.get(home_id), params.league_rating, home_shrink)
    home_def = _blend(params.defensive_rating.get(home_id), params.league_rating, home_shrink)
    away_off = _blend(params.offensive_rating.get(away_id), params.league_rating, away_shrink)
    away_def = _blend(params.defensive_rating.get(away_id), params.league_rating, away_shrink)

    # Pace is a property of the matchup, not either side alone.
    home_pace = _blend(params.pace.get(home_id), params.league_pace, home_shrink)
    away_pace = _blend(params.pace.get(away_id), params.league_pace, away_shrink)
    possessions = (home_pace + away_pace) / 2

    home_efficiency = home_off + away_def - params.league_rating
    away_efficiency = away_off + home_def - params.league_rating

    home_points = home_efficiency * possessions / 100
    away_points = away_efficiency * possessions / 100

    if not neutral_venue:
        home_points += params.home_advantage / 2
        away_points -= params.home_advantage / 2

    # Fatigue: a back-to-back costs more than a short rest alone.
    home_points += _rest_points(home_rest_days, home_back_to_back)
    away_points += _rest_points(away_rest_days, away_back_to_back)
    # Visiting teams fade at altitude (Denver is the standing example).
    if altitude_m > 1500 and not neutral_venue:
        away_points -= 1.5

    confidence, score = _assess_confidence(
        params.games_per_team.get(home_id, 0), params.games_per_team.get(away_id, 0)
    )
    return BasketballProjection(
        expected_margin=home_points - away_points,
        expected_total=home_points + away_points,
        margin_sd=params.margin_sd,
        total_sd=params.total_sd,
        home_points=home_points,
        away_points=away_points,
        confidence=confidence,
        confidence_score=score,
        detail={
            "possessions": possessions,
            "home_offensive_rating": home_off,
            "away_offensive_rating": away_off,
            "home_defensive_rating": home_def,
            "away_defensive_rating": away_def,
        },
    )


def _decay_weights(played: list[date], as_of: date, half_life_days: float) -> np.ndarray:
    ages = np.maximum(np.array([(as_of - d).days for d in played], dtype=float), 0.0)
    weights: np.ndarray = np.power(0.5, ages / half_life_days)
    return weights


def _weighted_mean(pairs: list[tuple[float, float]]) -> float:
    values = np.array([v for v, _ in pairs])
    weights = np.array([w for _, w in pairs])
    return float(np.average(values, weights=weights))


def _blend(team_value: float | None, league_value: float, weight: float) -> float:
    if team_value is None:
        return league_value
    return weight * team_value + (1 - weight) * league_value


def _shrinkage(n_games: int, prior_strength: float = 10.0) -> float:
    return n_games / (n_games + prior_strength) if n_games > 0 else 0.0


def _rest_points(rest_days: int | None, back_to_back: bool) -> float:
    if back_to_back:
        return -1.8
    if rest_days is None:
        return 0.0
    if rest_days >= 3:
        return 0.4
    return 0.0


def _assess_confidence(home_games: int, away_games: int) -> tuple[Confidence, float]:
    fewest = min(home_games, away_games)
    score = min(fewest / 25.0, 1.0)
    if fewest >= 20:
        return Confidence.HIGH, score
    if fewest >= 8:
        return Confidence.MEDIUM, score
    return Confidence.LOW, score
