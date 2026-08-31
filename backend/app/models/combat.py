"""Combat sports: MMA and boxing.

**Read this before trusting any number here.** Combat sports have the thinnest
data of any sport in this app. Fighters compete two or three times a year, so
even an established career is a sample of twenty-odd bouts; styles interact in
ways aggregate statistics capture poorly; and for boxing there is no free
structured source at all, so coverage is sparse and often just an event listing.

The model is therefore built to be honest rather than confident:

- ratings are Glicko-style, carrying an explicit uncertainty that *grows with
  inactivity* instead of pretending a fighter who last competed two years ago
  is as well understood as one who fought last month;
- method and round distributions come from the fighters' own finishing
  tendencies, with heavy regression to the division baseline;
- confidence is capped below HIGH for every bout, so combat selections face a
  higher edge bar in the bet builder and never qualify for the low-risk tier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from app.core.logging import get_logger
from app.db.enums import Confidence, VictoryMethod

log = get_logger(__name__)

MODEL_NAME = "combat-glicko-style"
MODEL_VERSION = "1.0.0"

DEFAULT_RATING = 1500.0
DEFAULT_DEVIATION = 350.0  # Glicko RD: how unsure we are of the rating
MIN_DEVIATION = 60.0
# Rating uncertainty grows this much per year out of competition.
INACTIVITY_RD_PER_YEAR = 90.0

# Division baselines for finish rates. Heavier divisions finish more often;
# these are the priors every fighter regresses toward.
DIVISION_FINISH_RATE = {
    "heavyweight": 0.62,
    "light_heavyweight": 0.55,
    "middleweight": 0.50,
    "welterweight": 0.46,
    "lightweight": 0.44,
    "featherweight": 0.42,
    "bantamweight": 0.38,
    "flyweight": 0.33,
    "strawweight": 0.28,
}
DEFAULT_FINISH_RATE = 0.45


@dataclass(frozen=True)
class Bout:
    """One historical bout, from the perspective of both fighters."""

    winner_id: int | None  # None for a draw or no contest
    fighter_a_id: int
    fighter_b_id: int
    fought_on: date
    method: VictoryMethod | None = None
    end_round: int | None = None
    scheduled_rounds: int = 3


@dataclass
class FighterRating:
    rating: float = DEFAULT_RATING
    deviation: float = DEFAULT_DEVIATION
    last_fought: date | None = None
    bouts: int = 0
    finishes: int = 0
    finished_losses: int = 0


@dataclass
class CombatParameters:
    ratings: dict[int, FighterRating] = field(default_factory=dict)
    division: str = "lightweight"

    def rating_for(self, fighter_id: int) -> FighterRating:
        return self.ratings.get(fighter_id, FighterRating())

    def as_hyperparameters(self) -> dict[str, float | int]:
        return {
            "n_fighters": len(self.ratings),
            "default_deviation": DEFAULT_DEVIATION,
            "inactivity_rd_per_year": INACTIVITY_RD_PER_YEAR,
        }


@dataclass(frozen=True)
class MethodDistribution:
    ko_tko: float
    submission: float
    decision: float
    draw: float

    def as_dict(self) -> dict[str, float]:
        return {
            "ko_tko": self.ko_tko,
            "submission": self.submission,
            "decision": self.decision,
            "draw": self.draw,
        }


@dataclass(frozen=True)
class CombatProjection:
    win_probability_a: float
    win_probability_b: float
    draw_probability: float
    method_a: MethodDistribution
    method_b: MethodDistribution
    # Probability the bout ends in each round, index 0 = round 1.
    round_finish_probabilities: list[float]
    distance_probability: float
    confidence: Confidence
    confidence_score: float
    detail: dict[str, float]
    caveat: str


def fit(bouts: list[Bout], as_of: date, division: str = "lightweight") -> CombatParameters:
    """Walk the bout history forward, updating ratings as results land."""
    params = CombatParameters(division=division)
    for bout in sorted(bouts, key=lambda b: b.fought_on):
        _update(params, bout)
    # Inflate uncertainty for anyone inactive since their last bout.
    for rating in params.ratings.values():
        if rating.last_fought is not None:
            rating.deviation = _decayed_deviation(rating.deviation, rating.last_fought, as_of)
    return params


def _update(params: CombatParameters, bout: Bout) -> None:
    a = params.ratings.setdefault(bout.fighter_a_id, FighterRating())
    b = params.ratings.setdefault(bout.fighter_b_id, FighterRating())

    # Uncertainty carried into this bout, from time since each last fought.
    for fighter in (a, b):
        if fighter.last_fought is not None:
            fighter.deviation = _decayed_deviation(
                fighter.deviation, fighter.last_fought, bout.fought_on
            )

    expected_a = _expected_score(a, b)
    # A draw or no contest scores half for both fighters.
    score_a = 0.5 if bout.winner_id is None else float(bout.winner_id == bout.fighter_a_id)

    # Glicko-style update: a more uncertain rating moves further.
    for fighter, score, expected in ((a, score_a, expected_a), (b, 1 - score_a, 1 - expected_a)):
        g = _g(fighter.deviation)
        variance = 1.0 / (g**2 * expected * (1 - expected)) if 0 < expected < 1 else 1e6
        denominator = 1.0 / fighter.deviation**2 + 1.0 / variance
        fighter.rating += (g / denominator) * (score - expected) * 173.7178
        fighter.deviation = max(math.sqrt(1.0 / denominator), MIN_DEVIATION)
        fighter.last_fought = bout.fought_on
        fighter.bouts += 1

    if bout.method in {VictoryMethod.KO_TKO, VictoryMethod.SUBMISSION} and bout.winner_id:
        winner = a if bout.winner_id == bout.fighter_a_id else b
        loser = b if bout.winner_id == bout.fighter_a_id else a
        winner.finishes += 1
        loser.finished_losses += 1


def project(
    params: CombatParameters,
    fighter_a_id: int,
    fighter_b_id: int,
    scheduled_rounds: int = 3,
    *,
    a_short_notice: bool = False,
    b_short_notice: bool = False,
    a_missed_weight: bool = False,
    b_missed_weight: bool = False,
) -> CombatProjection:
    """Project the bout: winner, method and round.

    Short-notice replacements and weight misses are explicit inputs because
    they matter a great deal and appear in no free data feed.
    """
    a = params.rating_for(fighter_a_id)
    b = params.rating_for(fighter_b_id)

    win_a = _expected_score(a, b)
    if a_short_notice:
        win_a *= 0.88
    if b_short_notice:
        win_a = 1 - (1 - win_a) * 0.88
    if a_missed_weight:
        win_a *= 0.94
    if b_missed_weight:
        win_a = 1 - (1 - win_a) * 0.94

    # Draws are rare but real; more likely in scored, longer bouts.
    draw = 0.015 if scheduled_rounds <= 3 else 0.025
    remaining = 1 - draw
    win_a = min(max(win_a, 0.02), 0.98)
    prob_a, prob_b = remaining * win_a, remaining * (1 - win_a)

    finish_rate = _finish_rate(params, a, b)
    method_a = _method_distribution(prob_a, finish_rate, a)
    method_b = _method_distribution(prob_b, finish_rate, b)

    total_finish = method_a.ko_tko + method_a.submission + method_b.ko_tko + method_b.submission
    round_probabilities = _round_distribution(total_finish, scheduled_rounds)

    confidence, score = _assess_confidence(a, b)
    return CombatProjection(
        win_probability_a=prob_a,
        win_probability_b=prob_b,
        draw_probability=draw,
        method_a=method_a,
        method_b=method_b,
        round_finish_probabilities=round_probabilities,
        distance_probability=1.0 - total_finish,
        confidence=confidence,
        confidence_score=score,
        detail={
            "rating_a": a.rating,
            "rating_b": b.rating,
            "deviation_a": a.deviation,
            "deviation_b": b.deviation,
            "finish_rate": finish_rate,
        },
        caveat=(
            "Combat sports carry the widest uncertainty in this app: few bouts per "
            "fighter, style matchups that statistics capture poorly, and sparse "
            "public data. Treat these probabilities as rough."
        ),
    )


def _round_distribution(total_finish_probability: float, scheduled_rounds: int) -> list[float]:
    """Discrete hazard model: probability of finishing in each round, given the
    bout reached it. Early rounds carry a higher hazard — fighters are fresh and
    take more risks — and it declines through the bout."""
    if scheduled_rounds <= 0 or total_finish_probability <= 0:
        return []

    hazards = [0.30, 0.25, 0.20, 0.15, 0.12][:scheduled_rounds]
    while len(hazards) < scheduled_rounds:
        hazards.append(0.10)

    # Scale hazards so their cumulative finish probability matches the model's.
    survival = 1.0
    raw = []
    for hazard in hazards:
        raw.append(survival * hazard)
        survival *= 1 - hazard
    raw_total = sum(raw)
    if raw_total <= 0:
        return [0.0] * scheduled_rounds
    return [p / raw_total * total_finish_probability for p in raw]


def _method_distribution(
    win_probability: float, finish_rate: float, fighter: FighterRating
) -> MethodDistribution:
    """Split a fighter's win probability across KO/TKO, submission and decision."""
    # A fighter's own finishing history, regressed hard toward the division
    # baseline — twenty bouts is not enough to trust a personal rate.
    personal = fighter.finishes / fighter.bouts if fighter.bouts > 0 else finish_rate
    weight = min(fighter.bouts / 25.0, 0.5)
    rate = weight * personal + (1 - weight) * finish_rate

    finishes = win_probability * rate
    return MethodDistribution(
        ko_tko=finishes * 0.65,  # KO is the more common finish overall
        submission=finishes * 0.35,
        decision=win_probability * (1 - rate),
        draw=0.0,
    )


def _finish_rate(params: CombatParameters, a: FighterRating, b: FighterRating) -> float:
    baseline = DIVISION_FINISH_RATE.get(params.division, DEFAULT_FINISH_RATE)
    total_bouts = a.bouts + b.bouts
    if total_bouts == 0:
        return baseline
    observed = (a.finishes + b.finishes + a.finished_losses + b.finished_losses) / (2 * total_bouts)
    weight = min(total_bouts / 40.0, 0.5)
    return weight * observed + (1 - weight) * baseline


def _expected_score(a: FighterRating, b: FighterRating) -> float:
    """Glicko expected score, widened by the pair's combined uncertainty.

    Two lightly-raced fighters produce a probability closer to a coin flip than
    their raw ratings suggest — which is the honest answer.
    """
    combined_rd = math.sqrt(a.deviation**2 + b.deviation**2)
    g = _g(combined_rd)
    return 1.0 / (1.0 + 10 ** (-g * (a.rating - b.rating) / 400.0))


def _g(deviation: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * (deviation / math.pi / 173.7178) ** 2)


def _decayed_deviation(deviation: float, last_fought: date, as_of: date) -> float:
    """Uncertainty grows with time out of competition, capped at the prior."""
    years = max((as_of - last_fought).days, 0) / 365.25
    grown = math.sqrt(deviation**2 + (INACTIVITY_RD_PER_YEAR**2) * years)
    return min(grown, DEFAULT_DEVIATION)


def _assess_confidence(a: FighterRating, b: FighterRating) -> tuple[Confidence, float]:
    """Confidence is capped at MEDIUM for every bout.

    Deliberate: with this little data, HIGH would license the low-risk tier to
    stake real money on a fight, which the evidence never supports.
    """
    fewest = min(a.bouts, b.bouts)
    widest = max(a.deviation, b.deviation)
    score = min(fewest / 12.0, 1.0) * (1 - min(widest / DEFAULT_DEVIATION, 1.0) * 0.5)
    if fewest >= 6 and widest < 150:
        return Confidence.MEDIUM, score
    return Confidence.LOW, score


def simulate_bout(
    projection: CombatProjection, n_iterations: int, seed: int
) -> dict[str, np.ndarray]:
    """Draw winner, method and round for the bout.

    Produces the same aligned-draw structure as the team-sport simulator, so
    combat legs can be parlayed against the joint distribution too.
    """
    rng = np.random.default_rng(seed)
    outcomes = ["a", "b", "draw"]
    probabilities = [
        projection.win_probability_a,
        projection.win_probability_b,
        projection.draw_probability,
    ]
    total = sum(probabilities)
    winners = rng.choice(len(outcomes), size=n_iterations, p=[p / total for p in probabilities])

    rounds = np.zeros(n_iterations, dtype=int)  # 0 means went the distance
    round_probs = projection.round_finish_probabilities
    if round_probs:
        finish_draw = rng.random(n_iterations)
        cumulative = 0.0
        for index, probability in enumerate(round_probs, start=1):
            in_round = (finish_draw >= cumulative) & (finish_draw < cumulative + probability)
            rounds[in_round] = index
            cumulative += probability

    return {
        "winner": winners,  # 0 = a, 1 = b, 2 = draw
        "end_round": rounds,
        "went_distance": (rounds == 0).astype(int),
    }
