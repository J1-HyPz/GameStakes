"""Monte Carlo simulation engine.

The central design decision: predictions are distributions, and the *raw draws*
are kept, not just their summaries. Parlay pricing has to come from the joint
distribution — multiplying marginal probabilities systematically overprices
same-game combinations, because legs within a game correlate. Every candidate
leg is evaluated against the same iterations, so a parlay's probability is
simply the fraction of iterations where all its legs win.

Reproducibility: a simulation is identified by its (fixture, model version,
seed, iteration count). Given the same inputs it produces identical draws, and
`seed` is stored so any result can be recreated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_ITERATIONS = 20_000
MAX_ITERATIONS = 100_000


def seed_for(fixture_id: int, model_version_id: int) -> int:
    """Deterministic per-fixture seed, so a rerun reproduces the same draws."""
    return (fixture_id * 1_000_003 + model_version_id * 31) % (2**63 - 1)


@dataclass(frozen=True)
class SimulationResult:
    """Draws from one fixture's simulation, plus what identifies them.

    `draws` maps a quantity name to an array of length n_iterations —
    "home_score", "away_score", and any per-player quantities. Every array is
    aligned: index i of each is the same simulated world, which is what makes
    joint (correlated) probabilities computable.
    """

    fixture_id: int
    model_version_id: int
    seed: int
    n_iterations: int
    draws: dict[str, np.ndarray]

    def summary(self) -> dict[str, Any]:
        """Compact stats for storage and display alongside the draws."""
        out: dict[str, Any] = {"n_iterations": self.n_iterations}
        for name, values in self.draws.items():
            out[name] = {
                "mean": float(np.mean(values)),
                "sd": float(np.std(values)),
                "p10": float(np.percentile(values, 10)),
                "p50": float(np.percentile(values, 50)),
                "p90": float(np.percentile(values, 90)),
            }
        return out

    def probability(self, mask: np.ndarray) -> float:
        """Fraction of iterations where `mask` holds."""
        return float(np.mean(mask))

    def save(self, directory: Path) -> Path:
        """Persist draws compressed. Kept out of the database: 20k draws per
        fixture per quantity belong on disk, not in table rows."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"sim_{self.fixture_id}_{self.model_version_id}_{self.seed}.npz"
        np.savez_compressed(path, **self.draws)  # type: ignore[arg-type]
        return path

    @staticmethod
    def load_draws(path: Path) -> dict[str, np.ndarray]:
        with np.load(path) as data:
            return {key: data[key] for key in data.files}


def joint_probability(result: SimulationResult, masks: list[np.ndarray]) -> tuple[float, float]:
    """Probability that every leg wins, and the naive independent figure.

    Returning both makes the correlation effect visible rather than implicit:
    for same-game legs the honest joint probability is often well below the
    product of the marginals, and a builder that quietly used the product
    would look profitable while losing money.
    """
    if not masks:
        return 0.0, 0.0
    combined = np.logical_and.reduce(masks)
    joint = float(np.mean(combined))
    naive = float(np.prod([np.mean(m) for m in masks]))
    return joint, naive


class ScoreSimulator:
    """Simulates final scores for a team fixture from expected goals/points.

    Sport-specific models supply the expected values and the shape of the
    randomness; this class owns the mechanics of drawing and packaging them.
    """

    def __init__(self, n_iterations: int = DEFAULT_ITERATIONS):
        if not 0 < n_iterations <= MAX_ITERATIONS:
            raise ValueError(f"n_iterations must be in 1..{MAX_ITERATIONS}")
        self.n_iterations = n_iterations

    def simulate_poisson(
        self,
        fixture_id: int,
        model_version_id: int,
        home_lambda: float,
        away_lambda: float,
        rho: float = 0.0,
        seed: int | None = None,
    ) -> SimulationResult:
        """Bivariate Poisson scores with the Dixon-Coles low-score correction.

        Independent Poisson margins misprice 0-0, 1-0, 0-1 and 1-1, which are a
        large share of football outcomes. `rho` tilts those four cells and is
        fitted per league by the model, not guessed here.
        """
        rng = np.random.default_rng(
            seed if seed is not None else seed_for(fixture_id, model_version_id)
        )
        home = rng.poisson(home_lambda, self.n_iterations)
        away = rng.poisson(away_lambda, self.n_iterations)

        if rho != 0.0:
            home, away = _apply_dixon_coles(home, away, home_lambda, away_lambda, rho, rng)

        return SimulationResult(
            fixture_id=fixture_id,
            model_version_id=model_version_id,
            seed=seed if seed is not None else seed_for(fixture_id, model_version_id),
            n_iterations=self.n_iterations,
            draws={"home_score": home, "away_score": away},
        )

    def simulate_margin_total(
        self,
        fixture_id: int,
        model_version_id: int,
        expected_margin: float,
        expected_total: float,
        margin_sd: float,
        total_sd: float,
        seed: int | None = None,
        key_numbers: dict[int, float] | None = None,
    ) -> SimulationResult:
        """Margin-and-total simulation for basketball and American football.

        `key_numbers` re-weights specific margins — 3 and 7 are far more common
        in the NFL than their neighbours, and a plain normal misprices spreads
        sitting on them.
        """
        used_seed = seed if seed is not None else seed_for(fixture_id, model_version_id)
        rng = np.random.default_rng(used_seed)
        margin = rng.normal(expected_margin, margin_sd, self.n_iterations)
        total = rng.normal(expected_total, total_sd, self.n_iterations)

        margin_int = np.rint(margin).astype(int)
        if key_numbers:
            margin_int = _snap_to_key_numbers(margin_int, key_numbers, rng)

        total_int = np.rint(total).astype(int)
        total_int = np.maximum(total_int, np.abs(margin_int))
        # home - away = margin, home + away = total
        home = (total_int + margin_int) // 2
        away = total_int - home

        return SimulationResult(
            fixture_id=fixture_id,
            model_version_id=model_version_id,
            seed=used_seed,
            n_iterations=self.n_iterations,
            draws={
                "home_score": home,
                "away_score": away,
                "margin": home - away,
                "total": home + away,
            },
        )


def _apply_dixon_coles(
    home: np.ndarray,
    away: np.ndarray,
    home_lambda: float,
    away_lambda: float,
    rho: float,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    """Resample low scores in proportion to the Dixon-Coles tau adjustment.

    Rejection sampling keeps the correction exact without needing a closed-form
    bivariate draw: cells whose tau exceeds 1 are kept more often.
    """
    tau = _tau(home, away, home_lambda, away_lambda, rho)
    # tau can exceed 1 for some cells, so normalise by its max to get accept
    # probabilities, then resample the rejected iterations.
    accept_p = tau / max(float(np.max(tau)), 1.0)
    rejected = rng.random(len(home)) > accept_p
    attempts = 0
    while rejected.any() and attempts < 20:
        n = int(rejected.sum())
        new_home = rng.poisson(home_lambda, n)
        new_away = rng.poisson(away_lambda, n)
        new_tau = _tau(new_home, new_away, home_lambda, away_lambda, rho)
        new_accept = new_tau / max(float(np.max(tau)), 1.0)
        keep = rng.random(n) <= new_accept
        idx = np.flatnonzero(rejected)
        home[idx[keep]] = new_home[keep]
        away[idx[keep]] = new_away[keep]
        rejected[idx[keep]] = False
        attempts += 1
    return home, away


def _tau(
    home: np.ndarray,
    away: np.ndarray,
    home_lambda: float,
    away_lambda: float,
    rho: float,
) -> np.ndarray:
    """Dixon-Coles tau: adjusts only the four lowest-score cells."""
    tau = np.ones(len(home), dtype=float)
    tau[(home == 0) & (away == 0)] = 1 - home_lambda * away_lambda * rho
    tau[(home == 0) & (away == 1)] = 1 + home_lambda * rho
    tau[(home == 1) & (away == 0)] = 1 + away_lambda * rho
    tau[(home == 1) & (away == 1)] = 1 - rho
    return np.maximum(tau, 0.0)


def _snap_to_key_numbers(
    margins: np.ndarray, key_numbers: dict[int, float], rng: np.random.Generator
) -> np.ndarray:
    """Pull a share of near-miss margins onto key numbers.

    `key_numbers` maps an absolute margin to the extra probability mass it
    should carry; mass is taken from margins one point either side.
    """
    out = margins.copy()
    for key, weight in key_numbers.items():
        for sign in (1, -1):
            target = key * sign
            neighbours = np.flatnonzero((out == target + sign) | (out == target - sign))
            if neighbours.size == 0:
                continue
            n_move = int(neighbours.size * weight)
            if n_move > 0:
                chosen = rng.choice(neighbours, size=min(n_move, neighbours.size), replace=False)
                out[chosen] = target
    return out
