"""Prediction endpoints: the market board and distribution for a fixture."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import SessionDep
from app.betting.odds import DevigMethod, best_price, devig
from app.db.enums import Confidence
from app.db.models import Fixture, OddsSnapshot, Prediction, Simulation

router = APIRouter(tags=["predictions"])


class MarketRow(BaseModel):
    """One selection: what the model thinks, what the market charges."""

    market: str
    selection: str
    line: float | None
    model_probability: float
    fair_price: float | None
    best_price: float | None
    bookmaker: str | None
    implied_probability: float | None
    edge: float | None


class PredictionOut(BaseModel):
    fixture_id: int
    generated_at: datetime
    model_version_id: int
    confidence: Confidence
    confidence_score: float | None
    n_iterations: int | None
    seed: int | None
    summary: dict[str, Any]
    heatmap: list[list[float]]
    markets: list[MarketRow]
    reasoning: dict[str, Any]


@router.get("/fixtures/{fixture_id}/prediction")
async def fixture_prediction(fixture_id: int, session: SessionDep) -> PredictionOut:
    fixture = await session.get(Fixture, fixture_id)
    if fixture is None:
        raise HTTPException(status_code=404, detail=f"fixture {fixture_id} not found")

    predictions = (
        (
            await session.execute(
                select(Prediction)
                .where(Prediction.fixture_id == fixture_id)
                .order_by(Prediction.generated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not predictions:
        raise HTTPException(
            status_code=404,
            detail=(
                "no prediction for this fixture yet — it needs enough finished matches "
                "in the league to fit the model"
            ),
        )

    newest = predictions[0].generated_at
    current = [p for p in predictions if p.generated_at == newest]
    simulation = (
        await session.get(Simulation, current[0].simulation_id)
        if current[0].simulation_id
        else None
    )
    summary = simulation.summary if simulation else {}

    prices = await _best_prices(session, fixture_id)
    rows = [_market_row(p, prices) for p in current]
    rows.sort(key=lambda r: (r.edge is None, -(r.edge or 0)))

    reasoning = (current[0].extra or {}).get("detail", {})
    return PredictionOut(
        fixture_id=fixture_id,
        generated_at=newest,
        model_version_id=current[0].model_version_id,
        confidence=current[0].confidence,
        confidence_score=current[0].confidence_score,
        n_iterations=simulation.n_iterations if simulation else None,
        seed=simulation.seed if simulation else None,
        summary={k: v for k, v in summary.items() if k != "heatmap"},
        heatmap=summary.get("heatmap", []),
        markets=rows,
        reasoning=reasoning,
    )


async def _best_prices(
    session: SessionDep, fixture_id: int
) -> dict[tuple[str, str, Decimal | None], tuple[str, float, float | None]]:
    """Best price per selection, with the de-vigged probability of its market."""
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

    by_market: dict[tuple[str, Decimal | None], dict[str, dict[str, float]]] = {}
    for snapshot in snapshots:
        market_key = (snapshot.market, snapshot.line)
        selections = by_market.setdefault(market_key, {})
        books = selections.setdefault(snapshot.selection, {})
        if snapshot.bookmaker not in books:  # newest first
            books[snapshot.bookmaker] = float(snapshot.price_decimal)

    out: dict[tuple[str, str, Decimal | None], tuple[str, float, float | None]] = {}
    for (market, line), selections in by_market.items():
        names = list(selections)
        best = {name: best_price(selections[name]) for name in names}
        fair = devig([best[name][1] for name in names], method=DevigMethod.POWER)
        for name, fair_p in zip(names, fair, strict=True):
            bookmaker, price = best[name]
            out[(market, name, line)] = (bookmaker, price, fair_p)
    return out


def _market_row(
    prediction: Prediction,
    prices: dict[tuple[str, str, Decimal | None], tuple[str, float, float | None]],
) -> MarketRow:
    price_info = prices.get((prediction.market, prediction.selection, prediction.line))
    bookmaker = price = implied = edge = None
    if price_info is not None:
        bookmaker, price, implied = price_info
        if implied is not None:
            edge = prediction.probability - implied

    return MarketRow(
        market=prediction.market,
        selection=prediction.selection,
        line=float(prediction.line) if prediction.line is not None else None,
        model_probability=prediction.probability,
        fair_price=(
            float(prediction.fair_price_decimal)
            if prediction.fair_price_decimal is not None
            else None
        ),
        best_price=price,
        bookmaker=bookmaker,
        implied_probability=implied,
        edge=edge,
    )
