"""Turning fitted models into stored, reproducible predictions.

The pipeline: load history -> fit -> project -> simulate -> derive markets ->
persist. Every prediction row records the model version, the simulation (and
therefore the seed) and a hash of its inputs, so any number in the UI can be
traced back to what produced it and regenerated exactly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import Confidence, FixtureStatus, Side
from app.db.models import (
    Fixture,
    FixtureParticipant,
    League,
    ModelVersion,
    Prediction,
    Result,
    Simulation,
    Sport,
)
from app.models import football
from app.models.elo import EloModel
from app.sim.engine import DEFAULT_ITERATIONS, ScoreSimulator, seed_for
from app.sim.markets import MarketOutcome, football_markets, score_heatmap

log = get_logger(__name__)

# How far back to look when fitting. Two seasons balances sample size against
# squads that no longer resemble themselves.
TRAINING_WINDOW_DAYS = 730


@dataclass
class PredictionRun:
    fixture_id: int
    predictions: int
    confidence: Confidence
    simulation_id: int | None
    skipped_reason: str | None = None


def feature_hash(payload: dict[str, object]) -> str:
    """Stable hash of a prediction's inputs, for reproducibility checks."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class FootballPredictor:
    """Fits the football model for a league and predicts its fixtures."""

    def __init__(self, session: AsyncSession, n_iterations: int = DEFAULT_ITERATIONS):
        self.session = session
        self.n_iterations = n_iterations
        self.simulator = ScoreSimulator(n_iterations)

    async def run_league(
        self, league_slug: str, as_of: date | None = None, horizon_days: int = 14
    ) -> list[PredictionRun]:
        as_of = as_of or datetime.now(UTC).date()
        league = (
            await self.session.execute(select(League).where(League.slug == league_slug))
        ).scalar_one_or_none()
        if league is None:
            raise ValueError(f"unknown league '{league_slug}'")

        matches, elo = await self._history(league.id, as_of)
        if len(matches) < 20:
            log.warning("predict.insufficient_history", league=league_slug, matches=len(matches))
            return []

        params = football.fit(matches, as_of=as_of)
        model_version = await self._model_version(league.sport_id, params)

        upcoming = await self._upcoming(league.id, as_of, horizon_days)
        runs = []
        for fixture, home_id, away_id in upcoming:
            runs.append(
                await self._predict_fixture(fixture, home_id, away_id, params, elo, model_version)
            )
        await self.session.commit()
        return runs

    async def _predict_fixture(
        self,
        fixture: Fixture,
        home_id: int,
        away_id: int,
        params: football.FootballParameters,
        elo: EloModel,
        model_version: ModelVersion,
    ) -> PredictionRun:
        projection = football.project(params, home_id, away_id, neutral_venue=fixture.neutral_site)

        seed = seed_for(fixture.id, model_version.id)
        sim = self.simulator.simulate_poisson(
            fixture_id=fixture.id,
            model_version_id=model_version.id,
            home_lambda=projection.home_lambda,
            away_lambda=projection.away_lambda,
            rho=projection.rho,
            seed=seed,
        )

        outcomes = football_markets(sim)
        outcomes = self._blend_match_winner(outcomes, elo, home_id, away_id)

        artifact = sim.save(_artifact_dir())
        simulation = Simulation(
            fixture_id=fixture.id,
            model_version_id=model_version.id,
            seed=seed,
            n_iterations=sim.n_iterations,
            artifact_path=str(artifact),
            summary={**sim.summary(), "heatmap": score_heatmap(sim)},
        )
        self.session.add(simulation)
        await self.session.flush()

        inputs = feature_hash(
            {
                "home_lambda": round(projection.home_lambda, 6),
                "away_lambda": round(projection.away_lambda, 6),
                "rho": round(projection.rho, 6),
                "model_version": model_version.version,
                "n_iterations": sim.n_iterations,
            }
        )
        generated_at = datetime.now(UTC)

        await self._clear_previous(fixture.id, model_version.id)
        for outcome in outcomes:
            self.session.add(
                Prediction(
                    fixture_id=fixture.id,
                    model_version_id=model_version.id,
                    simulation_id=simulation.id,
                    market=outcome.market,
                    selection=outcome.selection,
                    line=outcome.line,
                    probability=outcome.probability,
                    fair_price_decimal=(
                        None if outcome.fair_price is None else round(outcome.fair_price, 3)
                    ),
                    confidence=projection.confidence,
                    confidence_score=projection.confidence_score,
                    feature_hash=inputs,
                    generated_at=generated_at,
                    extra={"detail": projection.detail} if outcome.market == "1x2" else {},
                )
            )

        return PredictionRun(
            fixture_id=fixture.id,
            predictions=len(outcomes),
            confidence=projection.confidence,
            simulation_id=simulation.id,
        )

    def _blend_match_winner(
        self, outcomes: list[MarketOutcome], elo: EloModel, home_id: int, away_id: int
    ) -> list[MarketOutcome]:
        """Ensemble the 1X2 probabilities with Elo, leaving masks intact.

        Only the reported probabilities blend — the masks stay as simulated,
        because parlay pricing must keep using the joint distribution.
        """
        match_outcomes = {o.selection: o for o in outcomes if o.market == "1x2"}
        if len(match_outcomes) != 3:
            return outcomes

        model_probs = (
            match_outcomes["home"].probability,
            match_outcomes["draw"].probability,
            match_outcomes["away"].probability,
        )
        elo_probs = elo.win_probabilities(home_id, away_id)
        blended = football.blend_with_elo(model_probs, elo_probs)

        updated = []
        for outcome in outcomes:
            if outcome.market == "1x2":
                index = {"home": 0, "draw": 1, "away": 2}[outcome.selection]
                updated.append(
                    MarketOutcome(
                        market=outcome.market,
                        selection=outcome.selection,
                        line=outcome.line,
                        probability=blended[index],
                        mask=outcome.mask,
                    )
                )
            else:
                updated.append(outcome)
        return updated

    async def _history(self, league_id: int, as_of: date) -> tuple[list[football.Match], EloModel]:
        """Finished fixtures with results, plus an Elo model walked forward.

        Only matches that kicked off before `as_of` are used — the
        no-lookahead rule the backtester depends on.
        """
        cutoff = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
        window_start = cutoff - timedelta(days=TRAINING_WINDOW_DAYS)

        stmt = (
            select(Fixture, Result)
            .join(Result, Result.fixture_id == Fixture.id)
            .where(
                Fixture.league_id == league_id,
                Fixture.status == FixtureStatus.FINISHED,
                Fixture.start_time < cutoff,
                Fixture.start_time >= window_start,
            )
            .order_by(Fixture.start_time)
        )
        rows = (await self.session.execute(stmt)).all()

        matches: list[football.Match] = []
        elo = EloModel()
        for fixture, result in rows:
            sides = await self._sides(fixture.id)
            if sides is None or result.home_score is None or result.away_score is None:
                continue
            home_id, away_id = sides
            matches.append(
                football.Match(
                    home_id=home_id,
                    away_id=away_id,
                    home_goals=result.home_score,
                    away_goals=result.away_score,
                    played_on=fixture.start_time.date(),
                    home_xg=(result.score_detail or {}).get("home_xg"),
                    away_xg=(result.score_detail or {}).get("away_xg"),
                )
            )
            elo.update(home_id, away_id, result.home_score, result.away_score)
        return matches, elo

    async def _upcoming(
        self, league_id: int, as_of: date, horizon_days: int
    ) -> list[tuple[Fixture, int, int]]:
        start = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
        stmt = (
            select(Fixture)
            .where(
                Fixture.league_id == league_id,
                Fixture.start_time >= start,
                Fixture.start_time < start + timedelta(days=horizon_days),
                Fixture.status == FixtureStatus.SCHEDULED,
            )
            .order_by(Fixture.start_time)
        )
        fixtures = (await self.session.execute(stmt)).scalars().all()

        out = []
        for fixture in fixtures:
            sides = await self._sides(fixture.id)
            if sides is not None:
                out.append((fixture, sides[0], sides[1]))
        return out

    async def _sides(self, fixture_id: int) -> tuple[int, int] | None:
        participants = (
            (
                await self.session.execute(
                    select(FixtureParticipant).where(FixtureParticipant.fixture_id == fixture_id)
                )
            )
            .scalars()
            .all()
        )
        home = next((p.team_id for p in participants if p.side == Side.HOME and p.team_id), None)
        away = next((p.team_id for p in participants if p.side == Side.AWAY and p.team_id), None)
        return (home, away) if home and away else None

    async def _model_version(
        self, sport_id: int, params: football.FootballParameters
    ) -> ModelVersion:
        existing = (
            await self.session.execute(
                select(ModelVersion).where(
                    ModelVersion.name == football.MODEL_NAME,
                    ModelVersion.version == football.MODEL_VERSION,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.hyperparameters = params.as_hyperparameters()
            return existing

        version = ModelVersion(
            sport_id=sport_id,
            name=football.MODEL_NAME,
            version=football.MODEL_VERSION,
            hyperparameters=params.as_hyperparameters(),
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def _clear_previous(self, fixture_id: int, model_version_id: int) -> None:
        """Replace this model's previous predictions for the fixture.

        History is kept per model version, so comparing versions stays possible
        while the fixture page shows one current number per market.
        """
        from sqlalchemy import delete

        await self.session.execute(
            delete(Prediction).where(
                Prediction.fixture_id == fixture_id,
                Prediction.model_version_id == model_version_id,
            )
        )


def _artifact_dir() -> Path:
    """Simulation draws live on the data volume, not in the database."""
    settings = get_settings()
    base = Path(settings.data_dir) / "simulations"
    return base


async def predict_all_leagues(session: AsyncSession, sport: str = "football") -> int:
    """Run predictions for every active league of a sport."""
    stmt = (
        select(League)
        .join(Sport, League.sport_id == Sport.id)
        .where(Sport.slug == sport, League.is_active.is_(True))
    )
    leagues = (await session.execute(stmt)).scalars().all()
    predictor = FootballPredictor(session)
    total = 0
    for league in leagues:
        try:
            runs = await predictor.run_league(league.slug)
            total += sum(r.predictions for r in runs)
        except Exception:  # noqa: BLE001 — one league must not stop the rest
            log.exception("predict.league_failed", league=league.slug)
    return total
