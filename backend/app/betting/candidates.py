"""Building the candidate leg universe for a slate.

Joins stored predictions to current prices, de-vigs each market so the
comparison is against a fair probability rather than a marked-up one, and
attaches the simulation mask that makes correlated pricing possible.

A leg without a mask cannot be parlayed honestly, so predictions whose
simulation draws are missing are dropped rather than silently priced as
independent.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.betting.odds import DevigMethod, best_price, devig
from app.betting.parlay import Leg
from app.core.logging import get_logger
from app.db.enums import Confidence, FixtureStatus
from app.db.models import Fixture, League, OddsSnapshot, Prediction, Simulation, Sport
from app.sim.engine import SimulationResult

log = get_logger(__name__)


async def build_candidates(
    session: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    sport: str | None = None,
    league: str | None = None,
    min_confidence: Confidence = Confidence.MEDIUM,
    devig_method: DevigMethod = DevigMethod.POWER,
) -> list[Leg]:
    """Every priced, positive-edge selection on the slate."""
    start = start or datetime.now(UTC)
    end = end or start + timedelta(days=2)

    stmt = (
        select(Fixture)
        .join(League, Fixture.league_id == League.id)
        .join(Sport, Fixture.sport_id == Sport.id)
        .where(
            Fixture.start_time >= start,
            Fixture.start_time <= end,
            Fixture.status == FixtureStatus.SCHEDULED,
        )
    )
    if sport:
        stmt = stmt.where(Sport.slug == sport)
    if league:
        stmt = stmt.where(League.slug == league)

    fixtures = (await session.execute(stmt)).scalars().all()
    legs: list[Leg] = []
    for fixture in fixtures:
        legs.extend(await _fixture_candidates(session, fixture, min_confidence, devig_method))
    log.info("candidates.built", fixtures=len(fixtures), legs=len(legs))
    return legs


async def _fixture_candidates(
    session: AsyncSession,
    fixture: Fixture,
    min_confidence: Confidence,
    devig_method: DevigMethod,
) -> list[Leg]:
    predictions = (
        (
            await session.execute(
                select(Prediction)
                .where(Prediction.fixture_id == fixture.id)
                .order_by(Prediction.generated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not predictions:
        return []

    # Newest generation only — older rows are history, not current opinion.
    newest = predictions[0].generated_at
    predictions = [p for p in predictions if p.generated_at == newest]

    if _confidence_rank(predictions[0].confidence) < _confidence_rank(min_confidence):
        return []

    draws = await _load_draws(session, predictions[0].simulation_id)
    if draws is None:
        log.warning("candidates.no_draws", fixture_id=fixture.id)
        return []

    prices = await _prices_by_market(session, fixture.id)
    if not prices:
        return []

    legs: list[Leg] = []
    for (market, line), selections in prices.items():
        # De-vig across the selections of one market so the comparison is
        # against a fair price, not the marked-up one.
        names = list(selections)
        fair = devig([selections[name][1] for name in names], method=devig_method)
        fair_by_selection = dict(zip(names, fair, strict=True))

        for selection, (bookmaker, price) in selections.items():
            prediction = _match_prediction(predictions, market, selection, line)
            if prediction is None:
                continue
            mask = _mask_for(draws, prediction)
            if mask is None:
                continue

            legs.append(
                Leg(
                    fixture_id=fixture.id,
                    market=market,
                    selection=selection,
                    line=line,
                    decimal_odds=price,
                    model_probability=prediction.probability,
                    devigged_probability=fair_by_selection[selection],
                    bookmaker=bookmaker,
                    mask=mask,
                    player_id=prediction.player_id,
                    confidence=prediction.confidence.value,
                )
            )
    return legs


async def _prices_by_market(
    session: AsyncSession, fixture_id: int
) -> dict[tuple[str, Decimal | None], dict[str, tuple[str, float]]]:
    """Best current price per selection, grouped by market and line."""
    snapshots = (
        (
            await session.execute(
                select(OddsSnapshot)
                .where(OddsSnapshot.fixture_id == fixture_id)
                .order_by(OddsSnapshot.captured_at.desc())
            )
        )
        .scalars()
        .all()
    )

    # market -> selection -> bookmaker -> newest price
    seen: dict[tuple[str, Decimal | None], dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for snapshot in snapshots:
        book_prices = seen[(snapshot.market, snapshot.line)][snapshot.selection]
        if snapshot.bookmaker not in book_prices:  # newest first
            book_prices[snapshot.bookmaker] = float(snapshot.price_decimal)

    out: dict[tuple[str, Decimal | None], dict[str, tuple[str, float]]] = {}
    for key, selections in seen.items():
        # Line shopping: take the best available price for each selection.
        out[key] = {
            selection: best_price(books) for selection, books in selections.items() if books
        }
    return out


def _match_prediction(
    predictions: list[Prediction], market: str, selection: str, line: Decimal | None
) -> Prediction | None:
    for prediction in predictions:
        if (
            prediction.market == market
            and prediction.selection == selection
            and prediction.line == line
        ):
            return prediction
    return None


async def _load_draws(
    session: AsyncSession, simulation_id: int | None
) -> dict[str, np.ndarray] | None:
    if simulation_id is None:
        return None
    simulation = await session.get(Simulation, simulation_id)
    if simulation is None or not simulation.artifact_path:
        return None
    path = Path(simulation.artifact_path)
    if not path.exists():
        return None
    draws: dict[str, np.ndarray] = SimulationResult.load_draws(path)
    return draws


def _mask_for(draws: dict[str, np.ndarray], prediction: Prediction) -> np.ndarray | None:
    """Rebuild the boolean mask for a stored prediction from its draws.

    Recomputed rather than stored: masks are derived data, and regenerating
    them from the persisted draws keeps predictions and pricing in lockstep.
    """
    home = draws.get("home_score")
    away = draws.get("away_score")
    if home is None or away is None:
        return None

    market, selection, line = prediction.market, prediction.selection, prediction.line
    total = home + away
    float_line = float(line) if line is not None else None

    if market == "1x2":
        return {"home": home > away, "draw": home == away, "away": away > home}.get(selection)
    if market == "double_chance":
        return {
            "home_or_draw": home >= away,
            "away_or_draw": away >= home,
            "home_or_away": home != away,
        }.get(selection)
    if market == "totals" and float_line is not None:
        return {"over": total > float_line, "under": total < float_line}.get(selection)
    if market == "btts":
        return {"yes": (home > 0) & (away > 0), "no": (home == 0) | (away == 0)}.get(selection)
    if market == "clean_sheet":
        return {"home": away == 0, "away": home == 0}.get(selection)
    if market == "handicap" and float_line is not None:
        return {"home": home + float_line > away, "away": home < away + float_line}.get(selection)
    if market == "spreads" and float_line is not None:
        margin = draws.get("margin", home - away)
        return {"home": margin + float_line > 0, "away": margin + float_line < 0}.get(selection)
    if market == "team_total_home" and float_line is not None:
        return {"over": home > float_line, "under": home < float_line}.get(selection)
    if market == "team_total_away" and float_line is not None:
        return {"over": away > float_line, "under": away < float_line}.get(selection)
    if market == "correct_score" and "-" in selection:
        try:
            h, a = (int(part) for part in selection.split("-"))
        except ValueError:
            return None
        exact: np.ndarray = (home == h) & (away == a)
        return exact
    return None


def _confidence_rank(confidence: Confidence | str) -> int:
    value = confidence.value if isinstance(confidence, Confidence) else confidence
    return {"low": 0, "medium": 1, "high": 2}.get(value, 0)
