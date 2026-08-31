"""American football model: EPA-based ratings with key-number aware margins.

Two things distinguish this from a generic margin model:

**Key numbers.** NFL margins cluster hard on 3 and 7 because of how scoring
works. A plain normal distribution spreads probability smoothly and therefore
misprices every spread sitting on those numbers — which is most of them. The
simulator re-weights margins onto key numbers explicitly.

**Per-league spread.** NFL margin SD sits near 13.5 points; college football is
much wider because talent gaps are larger. Both are fitted from data, with the
priors below used only until there is enough history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

from app.core.logging import get_logger
from app.db.enums import Confidence

log = get_logger(__name__)

MODEL_NAME = "american-football-epa"
MODEL_VERSION = "1.0.0"

LEAGUE_PRIORS = {
    "nfl": {"margin_sd": 13.5, "total": 44.0, "total_sd": 10.0, "home_advantage": 1.8},
    "ncaa-fbs": {"margin_sd": 17.5, "total": 55.0, "total_sd": 13.0, "home_advantage": 2.5},
    "ufl": {"margin_sd": 12.0, "total": 40.0, "total_sd": 9.5, "home_advantage": 1.5},
    "cfl": {"margin_sd": 13.0, "total": 48.0, "total_sd": 11.0, "home_advantage": 1.8},
}
DEFAULT_PRIOR = LEAGUE_PRIORS["nfl"]

# Extra probability mass pulled onto each key margin. Roughly matches the
# observed excess in NFL results over a smooth distribution.
NFL_KEY_NUMBERS = {3: 0.35, 7: 0.22, 10: 0.10, 14: 0.06}
NCAA_KEY_NUMBERS = {3: 0.22, 7: 0.16, 10: 0.08, 14: 0.05}


@dataclass(frozen=True)
class Game:
    home_id: int
    away_id: int
    home_points: int
    away_points: int
    played_on: date
    # Opponent-adjusted EPA per play, split by phase where available.
    home_epa_pass: float | None = None
    home_epa_rush: float | None = None
    away_epa_pass: float | None = None
    away_epa_rush: float | None = None


@dataclass
class FootballParameters:
    offence: dict[int, float] = field(default_factory=dict)
    defence: dict[int, float] = field(default_factory=dict)
    home_advantage: float = DEFAULT_PRIOR["home_advantage"]
    league_total: float = DEFAULT_PRIOR["total"]
    margin_sd: float = DEFAULT_PRIOR["margin_sd"]
    total_sd: float = DEFAULT_PRIOR["total_sd"]
    key_numbers: dict[int, float] = field(default_factory=lambda: dict(NFL_KEY_NUMBERS))
    games_per_team: dict[int, int] = field(default_factory=dict)

    def as_hyperparameters(self) -> dict[str, float | int]:
        return {
            "home_advantage": self.home_advantage,
            "league_total": self.league_total,
            "margin_sd": self.margin_sd,
            "total_sd": self.total_sd,
            "n_teams": len(self.offence),
        }


@dataclass(frozen=True)
class FootballProjection:
    expected_margin: float
    expected_total: float
    margin_sd: float
    total_sd: float
    key_numbers: dict[int, float]
    confidence: Confidence
    confidence_score: float
    detail: dict[str, float]


def fit(
    games: list[Game], as_of: date, league: str = "nfl", half_life_days: float = 120.0
) -> FootballParameters:
    """Fit team scoring strength and the league's margin distribution.

    A longer half-life than basketball: there are only 17 regular-season games,
    so discarding older results leaves nothing to fit on.
    """
    prior = LEAGUE_PRIORS.get(league, DEFAULT_PRIOR)
    key_numbers = dict(NCAA_KEY_NUMBERS if league.startswith("ncaa") else NFL_KEY_NUMBERS)

    if len(games) < 16:
        return FootballParameters(
            home_advantage=prior["home_advantage"],
            league_total=prior["total"],
            margin_sd=prior["margin_sd"],
            total_sd=prior["total_sd"],
            key_numbers=key_numbers,
        )

    weights = _decay_weights([g.played_on for g in games], as_of, half_life_days)
    totals = np.array([g.home_points + g.away_points for g in games], dtype=float)
    margins = np.array([g.home_points - g.away_points for g in games], dtype=float)

    league_total = float(np.average(totals, weights=weights))
    home_advantage = float(np.average(margins, weights=weights))
    margin_sd = float(np.sqrt(np.average((margins - home_advantage) ** 2, weights=weights)))
    total_sd = float(np.sqrt(np.average((totals - league_total) ** 2, weights=weights)))

    scored: dict[int, list[tuple[float, float]]] = {}
    allowed: dict[int, list[tuple[float, float]]] = {}
    counts: dict[int, int] = {}
    baseline = league_total / 2

    for game, weight in zip(games, weights, strict=True):
        for team, points, against in (
            (game.home_id, game.home_points, game.away_points),
            (game.away_id, game.away_points, game.home_points),
        ):
            scored.setdefault(team, []).append((points - baseline, weight))
            allowed.setdefault(team, []).append((baseline - against, weight))
            counts[team] = counts.get(team, 0) + 1

    return FootballParameters(
        offence={t: _weighted_mean(v) for t, v in scored.items()},
        defence={t: _weighted_mean(v) for t, v in allowed.items()},
        home_advantage=home_advantage,
        league_total=league_total,
        margin_sd=margin_sd,
        total_sd=total_sd,
        key_numbers=key_numbers,
        games_per_team=counts,
    )


def project(
    params: FootballParameters,
    home_id: int,
    away_id: int,
    *,
    neutral_venue: bool = False,
    home_rest_days: int | None = None,
    away_rest_days: int | None = None,
    wind_mph: float = 0.0,
    precipitation: bool = False,
    altitude_m: int = 0,
    is_dome: bool = False,
) -> FootballProjection:
    """Project margin and total, adjusted for rest and weather.

    Wind is the weather variable that matters most: it suppresses the passing
    game and therefore the total, while rain affects it far less than intuition
    suggests. Domes are exempt.
    """
    home_shrink = _shrinkage(params.games_per_team.get(home_id, 0))
    away_shrink = _shrinkage(params.games_per_team.get(away_id, 0))

    home_off = params.offence.get(home_id, 0.0) * home_shrink
    home_def = params.defence.get(home_id, 0.0) * home_shrink
    away_off = params.offence.get(away_id, 0.0) * away_shrink
    away_def = params.defence.get(away_id, 0.0) * away_shrink

    baseline = params.league_total / 2
    home_points = baseline + home_off - away_def
    away_points = baseline + away_off - home_def

    if not neutral_venue:
        home_points += params.home_advantage / 2
        away_points -= params.home_advantage / 2
        if altitude_m > 1500:
            away_points -= 1.0

    home_points += _rest_adjustment(home_rest_days)
    away_points += _rest_adjustment(away_rest_days)

    if not is_dome:
        wind_penalty = _wind_penalty(wind_mph)
        home_points -= wind_penalty / 2
        away_points -= wind_penalty / 2
        if precipitation:
            home_points -= 0.75
            away_points -= 0.75

    confidence, score = _assess_confidence(
        params.games_per_team.get(home_id, 0), params.games_per_team.get(away_id, 0)
    )
    return FootballProjection(
        expected_margin=home_points - away_points,
        expected_total=home_points + away_points,
        margin_sd=params.margin_sd,
        total_sd=params.total_sd,
        key_numbers=params.key_numbers,
        confidence=confidence,
        confidence_score=score,
        detail={
            "home_offence": home_off,
            "away_offence": away_off,
            "home_defence": home_def,
            "away_defence": away_def,
            "wind_penalty": _wind_penalty(wind_mph) if not is_dome else 0.0,
        },
    )


def _wind_penalty(wind_mph: float) -> float:
    """Points removed from the total by wind. Negligible below 10mph, then
    roughly linear as the passing game degrades."""
    if wind_mph < 10:
        return 0.0
    return min((wind_mph - 10) * 0.35, 7.0)


def _decay_weights(played: list[date], as_of: date, half_life_days: float) -> np.ndarray:
    ages = np.maximum(np.array([(as_of - d).days for d in played], dtype=float), 0.0)
    weights: np.ndarray = np.power(0.5, ages / half_life_days)
    return weights


def _weighted_mean(pairs: list[tuple[float, float]]) -> float:
    values = np.array([v for v, _ in pairs])
    weights = np.array([w for _, w in pairs])
    return float(np.average(values, weights=weights))


def _shrinkage(n_games: int, prior_strength: float = 6.0) -> float:
    return n_games / (n_games + prior_strength) if n_games > 0 else 0.0


def _rest_adjustment(rest_days: int | None) -> float:
    if rest_days is None:
        return 0.0
    if rest_days <= 4:  # Thursday game
        return -0.8
    if rest_days >= 13:  # off a bye
        return 0.6
    return 0.0


def _assess_confidence(home_games: int, away_games: int) -> tuple[Confidence, float]:
    """Confidence rises faster than in other sports because the season is short
    — waiting for 20 games would mean never predicting an NFL fixture."""
    fewest = min(home_games, away_games)
    score = min(fewest / 10.0, 1.0)
    if fewest >= 8:
        return Confidence.HIGH, score
    if fewest >= 4:
        return Confidence.MEDIUM, score
    return Confidence.LOW, score
