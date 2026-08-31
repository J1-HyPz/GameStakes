"""Deriving market probabilities from simulation draws.

Each market becomes a boolean mask over the iterations. Masks are the unit of
currency for the bet builder: a parlay's true probability is the fraction of
iterations where every leg's mask is true, which is only meaningful because
every mask is computed against the *same* draws.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from app.sim.engine import SimulationResult


@dataclass(frozen=True)
class MarketOutcome:
    """One selectable outcome with its model probability and its mask."""

    market: str
    selection: str
    line: Decimal | None
    probability: float
    mask: np.ndarray

    @property
    def fair_price(self) -> float | None:
        """Break-even decimal odds. None when the outcome never occurred, since
        1/0 is not a price — it is missing evidence."""
        return 1.0 / self.probability if self.probability > 0 else None


def _outcome(
    market: str, selection: str, mask: np.ndarray, line: Decimal | None = None
) -> MarketOutcome:
    return MarketOutcome(
        market=market,
        selection=selection,
        line=line,
        probability=float(np.mean(mask)),
        mask=mask,
    )


def match_winner(sim: SimulationResult, allow_draw: bool = True) -> list[MarketOutcome]:
    home, away = sim.draws["home_score"], sim.draws["away_score"]
    outcomes = [
        _outcome("1x2", "home", home > away),
        _outcome("1x2", "away", away > home),
    ]
    if allow_draw:
        outcomes.insert(1, _outcome("1x2", "draw", home == away))
    return outcomes


def double_chance(sim: SimulationResult) -> list[MarketOutcome]:
    home, away = sim.draws["home_score"], sim.draws["away_score"]
    return [
        _outcome("double_chance", "home_or_draw", home >= away),
        _outcome("double_chance", "away_or_draw", away >= home),
        _outcome("double_chance", "home_or_away", home != away),
    ]


def totals(sim: SimulationResult, lines: list[float]) -> list[MarketOutcome]:
    """Over/under. Whole-number lines can push, so 'under' is strictly less
    than — a total landing exactly on the line is neither a win nor a loss and
    is handled at settlement."""
    total = sim.draws["home_score"] + sim.draws["away_score"]
    outcomes = []
    for line in lines:
        outcomes.append(_outcome("totals", "over", total > line, Decimal(str(line))))
        outcomes.append(_outcome("totals", "under", total < line, Decimal(str(line))))
    return outcomes


def both_teams_to_score(sim: SimulationResult) -> list[MarketOutcome]:
    home, away = sim.draws["home_score"], sim.draws["away_score"]
    return [
        _outcome("btts", "yes", (home > 0) & (away > 0)),
        _outcome("btts", "no", (home == 0) | (away == 0)),
    ]


def clean_sheet(sim: SimulationResult) -> list[MarketOutcome]:
    home, away = sim.draws["home_score"], sim.draws["away_score"]
    return [
        _outcome("clean_sheet", "home", away == 0),
        _outcome("clean_sheet", "away", home == 0),
    ]


def asian_handicap(sim: SimulationResult, lines: list[float]) -> list[MarketOutcome]:
    """Handicap applied to the home side: home + line vs away."""
    home, away = sim.draws["home_score"], sim.draws["away_score"]
    outcomes = []
    for line in lines:
        adjusted = home + line
        outcomes.append(_outcome("handicap", "home", adjusted > away, Decimal(str(line))))
        outcomes.append(_outcome("handicap", "away", adjusted < away, Decimal(str(-line))))
    return outcomes


def correct_score(sim: SimulationResult, max_goals: int = 4) -> list[MarketOutcome]:
    """Exact scorelines up to `max_goals` each, plus an 'any other' bucket."""
    home, away = sim.draws["home_score"], sim.draws["away_score"]
    outcomes = []
    covered = np.zeros(len(home), dtype=bool)
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            mask = (home == h) & (away == a)
            covered |= mask
            outcomes.append(_outcome("correct_score", f"{h}-{a}", mask))
    outcomes.append(_outcome("correct_score", "any_other", ~covered))
    return outcomes


def team_totals(sim: SimulationResult, lines: list[float]) -> list[MarketOutcome]:
    home, away = sim.draws["home_score"], sim.draws["away_score"]
    outcomes = []
    for line in lines:
        decimal_line = Decimal(str(line))
        outcomes.append(_outcome("team_total_home", "over", home > line, decimal_line))
        outcomes.append(_outcome("team_total_home", "under", home < line, decimal_line))
        outcomes.append(_outcome("team_total_away", "over", away > line, decimal_line))
        outcomes.append(_outcome("team_total_away", "under", away < line, decimal_line))
    return outcomes


def spreads(sim: SimulationResult, lines: list[float]) -> list[MarketOutcome]:
    """Point spreads for basketball and American football."""
    margin = sim.draws.get("margin")
    if margin is None:
        margin = sim.draws["home_score"] - sim.draws["away_score"]
    outcomes = []
    for line in lines:
        outcomes.append(_outcome("spreads", "home", margin + line > 0, Decimal(str(line))))
        outcomes.append(_outcome("spreads", "away", margin + line < 0, Decimal(str(-line))))
    return outcomes


def player_prop(
    sim: SimulationResult, player_key: str, lines: list[float], market: str
) -> list[MarketOutcome]:
    """Over/under on a per-player quantity drawn in the same iterations, so
    combined markets (points+rebounds+assists) and same-game parlays price
    against the real joint distribution."""
    values = sim.draws.get(player_key)
    if values is None:
        return []
    outcomes = []
    for line in lines:
        outcomes.append(
            MarketOutcome(
                market=market,
                selection=f"{player_key}_over",
                line=Decimal(str(line)),
                probability=float(np.mean(values > line)),
                mask=values > line,
            )
        )
        outcomes.append(
            MarketOutcome(
                market=market,
                selection=f"{player_key}_under",
                line=Decimal(str(line)),
                probability=float(np.mean(values < line)),
                mask=values < line,
            )
        )
    return outcomes


def football_markets(
    sim: SimulationResult,
    total_lines: list[float] | None = None,
    handicap_lines: list[float] | None = None,
) -> list[MarketOutcome]:
    """The standard football board derived from one set of draws."""
    total_lines = total_lines or [0.5, 1.5, 2.5, 3.5, 4.5]
    handicap_lines = handicap_lines or [-2.0, -1.5, -1.0, -0.5, 0.5, 1.0, 1.5, 2.0]
    return [
        *match_winner(sim, allow_draw=True),
        *double_chance(sim),
        *totals(sim, total_lines),
        *both_teams_to_score(sim),
        *clean_sheet(sim),
        *asian_handicap(sim, handicap_lines),
        *team_totals(sim, [0.5, 1.5, 2.5]),
        *correct_score(sim),
    ]


def score_heatmap(sim: SimulationResult, max_goals: int = 5) -> list[list[float]]:
    """Scoreline probability grid for the fixture page's heatmap."""
    home, away = sim.draws["home_score"], sim.draws["away_score"]
    n = len(home)
    return [
        [float(np.sum((home == h) & (away == a)) / n) for a in range(max_goals + 1)]
        for h in range(max_goals + 1)
    ]
