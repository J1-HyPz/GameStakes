"""Football (soccer) model: Dixon-Coles bivariate Poisson, ensembled with Elo.

Each team carries an attack and a defence strength; expected goals for a
fixture are the product of the attacking side's attack, the opponent's defence,
the league's baseline, and home advantage. The Dixon-Coles rho correction fixes
the low-score cells (0-0, 1-0, 0-1, 1-1) that independent Poisson misprices.

Two deliberate choices:

- **Recency weighting.** Matches decay exponentially with a per-league
  half-life, because a rating built on two-year-old form is a rating of a team
  that no longer exists.
- **Regression to the mean.** Strengths are shrunk toward the league average in
  proportion to how little data supports them, so a team with four matches
  played does not get an extreme rating that the simulator would treat as
  certain.

The output is always a distribution with a confidence score, never a point
estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from scipy.optimize import minimize

from app.core.logging import get_logger
from app.db.enums import Confidence

log = get_logger(__name__)

MODEL_NAME = "football-dixon-coles-elo"
MODEL_VERSION = "1.0.0"

# Sensible priors used when a league has too little history to fit.
DEFAULT_HOME_ADVANTAGE = 0.26  # log-scale; ~1.30x goals for the home side
DEFAULT_RHO = -0.05
DEFAULT_BASE_GOALS = 1.35


@dataclass(frozen=True)
class Match:
    """One historical result, as the fitter needs it."""

    home_id: int
    away_id: int
    home_goals: int
    away_goals: int
    played_on: date
    # Optional xG, blended in because it predicts better than goals over small
    # samples (a team scoring 1 from 3.0 xG is likelier unlucky than bad).
    home_xg: float | None = None
    away_xg: float | None = None


@dataclass
class FootballParameters:
    attack: dict[int, float] = field(default_factory=dict)
    defence: dict[int, float] = field(default_factory=dict)
    home_advantage: float = DEFAULT_HOME_ADVANTAGE
    rho: float = DEFAULT_RHO
    base_goals: float = DEFAULT_BASE_GOALS
    matches_per_team: dict[int, int] = field(default_factory=dict)
    half_life_days: float = 180.0

    def as_hyperparameters(self) -> dict[str, float | int]:
        return {
            "home_advantage": self.home_advantage,
            "rho": self.rho,
            "base_goals": self.base_goals,
            "half_life_days": self.half_life_days,
            "n_teams": len(self.attack),
        }


@dataclass(frozen=True)
class Projection:
    """Expected goals for a fixture, with an honest confidence assessment."""

    home_lambda: float
    away_lambda: float
    rho: float
    confidence: Confidence
    confidence_score: float
    detail: dict[str, float]


def decay_weights(played: list[date], as_of: date, half_life_days: float) -> np.ndarray:
    """Exponential recency weights. A match one half-life old counts half."""
    ages = np.array([(as_of - d).days for d in played], dtype=float)
    ages = np.maximum(ages, 0.0)
    weights: np.ndarray = np.power(0.5, ages / half_life_days)
    return weights


def fit(
    matches: list[Match],
    as_of: date,
    half_life_days: float = 180.0,
    xg_weight: float = 0.5,
) -> FootballParameters:
    """Fit attack/defence strengths by weighted maximum likelihood.

    `xg_weight` blends expected goals into the target where available: 0 uses
    goals only, 1 uses xG only. The default splits the difference, taking xG's
    lower variance without ignoring the goals that actually decided matches.
    """
    if not matches:
        return FootballParameters(half_life_days=half_life_days)

    team_ids = sorted({m.home_id for m in matches} | {m.away_id for m in matches})
    index = {team_id: i for i, team_id in enumerate(team_ids)}
    n_teams = len(team_ids)

    weights = decay_weights([m.played_on for m in matches], as_of, half_life_days)
    home_idx = np.array([index[m.home_id] for m in matches])
    away_idx = np.array([index[m.away_id] for m in matches])
    home_goals = np.array([_target(m.home_goals, m.home_xg, xg_weight) for m in matches])
    away_goals = np.array([_target(m.away_goals, m.away_xg, xg_weight) for m in matches])

    # Parameters: [attack(n), defence(n), home_advantage, rho]
    initial = np.concatenate(
        [np.zeros(n_teams), np.zeros(n_teams), [DEFAULT_HOME_ADVANTAGE, DEFAULT_RHO]]
    )

    def negative_log_likelihood(params: np.ndarray) -> float:
        attack = params[:n_teams]
        defence = params[n_teams : 2 * n_teams]
        home_adv, rho = params[-2], params[-1]

        log_home = attack[home_idx] - defence[away_idx] + home_adv
        log_away = attack[away_idx] - defence[home_idx]
        lambda_home = np.exp(np.clip(log_home, -3, 3)) * DEFAULT_BASE_GOALS
        lambda_away = np.exp(np.clip(log_away, -3, 3)) * DEFAULT_BASE_GOALS

        ll = (
            home_goals * np.log(lambda_home)
            - lambda_home
            + away_goals * np.log(lambda_away)
            - lambda_away
        )
        # Dixon-Coles adjustment applies to the four lowest-score cells only.
        tau = _tau_vector(home_goals, away_goals, lambda_home, lambda_away, rho)
        ll = ll + np.log(np.maximum(tau, 1e-10))
        return float(-np.sum(weights * ll))

    # Identifiability: attack and defence are only defined up to a shift, so
    # pin their means at zero.
    constraints = [
        {"type": "eq", "fun": lambda p: float(np.mean(p[:n_teams]))},
        {"type": "eq", "fun": lambda p: float(np.mean(p[n_teams : 2 * n_teams]))},
    ]
    bounds = [(-2.0, 2.0)] * (2 * n_teams) + [(-0.5, 1.0), (-0.3, 0.3)]

    result = minimize(
        negative_log_likelihood,
        initial,
        method="SLSQP",
        bounds=bounds,
        constraints=constraints,
        options={"maxiter": 300, "ftol": 1e-6},
    )
    if not result.success:
        log.warning("football.fit_not_converged", message=result.message)

    params = result.x
    counts: dict[int, int] = {}
    for match in matches:
        counts[match.home_id] = counts.get(match.home_id, 0) + 1
        counts[match.away_id] = counts.get(match.away_id, 0) + 1

    return FootballParameters(
        attack={team_id: float(params[index[team_id]]) for team_id in team_ids},
        defence={team_id: float(params[n_teams + index[team_id]]) for team_id in team_ids},
        home_advantage=float(params[-2]),
        rho=float(params[-1]),
        base_goals=DEFAULT_BASE_GOALS,
        matches_per_team=counts,
        half_life_days=half_life_days,
    )


def project(
    params: FootballParameters,
    home_id: int,
    away_id: int,
    *,
    neutral_venue: bool = False,
    home_rest_days: int | None = None,
    away_rest_days: int | None = None,
    home_absences: float = 0.0,
    away_absences: float = 0.0,
) -> Projection:
    """Expected goals for one fixture, with adjustments and a confidence score.

    `*_absences` is a 0-1 share of key-player unavailability, entered manually:
    no free provider supplies reliable injury data, so the model takes it as an
    explicit input rather than pretending to know.
    """
    shrink_home = _shrinkage(params.matches_per_team.get(home_id, 0))
    shrink_away = _shrinkage(params.matches_per_team.get(away_id, 0))

    attack_home = params.attack.get(home_id, 0.0) * shrink_home
    defence_home = params.defence.get(home_id, 0.0) * shrink_home
    attack_away = params.attack.get(away_id, 0.0) * shrink_away
    defence_away = params.defence.get(away_id, 0.0) * shrink_away

    home_adv = 0.0 if neutral_venue else params.home_advantage
    log_home = attack_home - defence_away + home_adv
    log_away = attack_away - defence_home

    log_home += _rest_adjustment(home_rest_days)
    log_away += _rest_adjustment(away_rest_days)
    # Absences cut attacking output roughly in proportion to what is missing.
    log_home += math.log(max(1.0 - 0.35 * home_absences, 0.4))
    log_away += math.log(max(1.0 - 0.35 * away_absences, 0.4))

    home_lambda = float(np.exp(np.clip(log_home, -3, 3)) * params.base_goals)
    away_lambda = float(np.exp(np.clip(log_away, -3, 3)) * params.base_goals)

    confidence, score = assess_confidence(
        params.matches_per_team.get(home_id, 0), params.matches_per_team.get(away_id, 0)
    )
    return Projection(
        home_lambda=home_lambda,
        away_lambda=away_lambda,
        rho=params.rho,
        confidence=confidence,
        confidence_score=score,
        detail={
            "attack_home": attack_home,
            "defence_home": defence_home,
            "attack_away": attack_away,
            "defence_away": defence_away,
            "home_advantage": home_adv,
            "shrinkage_home": shrink_home,
            "shrinkage_away": shrink_away,
        },
    )


def blend_with_elo(
    model_probs: tuple[float, float, float],
    elo_probs: tuple[float, float, float],
    elo_weight: float = 0.3,
) -> tuple[float, float, float]:
    """Weighted ensemble of the scoring model and Elo, renormalised.

    Elo is the steadier of the two on thin data; the scoring model is sharper
    when there is enough of it. A fixed weight is the honest default until
    there are enough settled predictions to fit one.
    """
    blended = tuple(
        (1 - elo_weight) * m + elo_weight * e for m, e in zip(model_probs, elo_probs, strict=True)
    )
    total = sum(blended)
    return (blended[0] / total, blended[1] / total, blended[2] / total)


def assess_confidence(home_matches: int, away_matches: int) -> tuple[Confidence, float]:
    """Data-sufficiency score. Low-confidence fixtures are flagged and excluded
    from bet building by default — a thin sample is not an edge."""
    fewest = min(home_matches, away_matches)
    score = min(fewest / 20.0, 1.0)
    if fewest >= 15:
        return Confidence.HIGH, score
    if fewest >= 6:
        return Confidence.MEDIUM, score
    return Confidence.LOW, score


def _target(goals: int, xg: float | None, xg_weight: float) -> float:
    if xg is None:
        return float(goals)
    return (1 - xg_weight) * goals + xg_weight * xg


def _shrinkage(n_matches: int, prior_strength: float = 8.0) -> float:
    """Weight on a team's own fitted strength versus the league mean."""
    return n_matches / (n_matches + prior_strength) if n_matches > 0 else 0.0


def _rest_adjustment(rest_days: int | None) -> float:
    """Short rest costs output; extra rest helps a little, then plateaus."""
    if rest_days is None:
        return 0.0
    if rest_days <= 2:
        return -0.08
    if rest_days <= 3:
        return -0.04
    if rest_days >= 7:
        return 0.02
    return 0.0


def _tau_vector(
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    lambda_home: np.ndarray,
    lambda_away: np.ndarray,
    rho: float,
) -> np.ndarray:
    """Dixon-Coles tau over arrays; only low-score cells are adjusted."""
    tau = np.ones_like(home_goals, dtype=float)
    h0 = np.isclose(home_goals, 0)
    h1 = np.isclose(home_goals, 1)
    a0 = np.isclose(away_goals, 0)
    a1 = np.isclose(away_goals, 1)
    tau = np.where(h0 & a0, 1 - lambda_home * lambda_away * rho, tau)
    tau = np.where(h0 & a1, 1 + lambda_home * rho, tau)
    tau = np.where(h1 & a0, 1 + lambda_away * rho, tau)
    tau = np.where(h1 & a1, 1 - rho, tau)
    return np.asarray(tau, dtype=float)
