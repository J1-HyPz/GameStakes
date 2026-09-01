"""Fixture and result ingestion.

Raw payloads land in `raw_ingest` before normalisation so models can be rebuilt
without refetching. Participants resolve through EntityResolver; a fixture
whose teams cannot be resolved is skipped and counted rather than guessed at,
and the unresolved names wait in the review queue.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import EntityType, FixtureStatus, Side
from app.db.models import (
    EntityAlias,
    Fixture,
    FixtureParticipant,
    League,
    RawIngest,
    Result,
    Sport,
    Team,
)
from app.ingest.jobs import track_job
from app.ingest.resolution import EntityResolver, normalize_name
from app.providers.base import RawFixture, RawResult
from app.providers.registry import ProviderRegistry, get_registry

log = get_logger(__name__)


async def store_raw(
    session: AsyncSession,
    provider: str,
    endpoint: str,
    params: dict[str, Any],
    payload: Any,
) -> RawIngest:
    """Persist a provider payload verbatim, deduplicated by content hash."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    content_hash = hashlib.sha256(blob.encode()).hexdigest()
    row = RawIngest(
        provider=provider,
        endpoint=endpoint,
        params=params,
        payload=payload,
        content_hash=content_hash,
        fetched_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


class FixtureIngestor:
    """Turns provider fixtures into canonical rows."""

    def __init__(self, session: AsyncSession, registry: ProviderRegistry | None = None):
        self.session = session
        self.registry = registry or get_registry()
        self.resolver = EntityResolver(session)

    async def ingest_league(self, league_slug: str, start: date, end: date) -> dict[str, int]:
        league = await self._league(league_slug)
        sport = await self.session.get(Sport, league.sport_id)
        assert sport is not None

        async with track_job(self.session, f"fixtures:{league_slug}") as job:
            outcome = await self.registry.fetch_fixtures(league_slug, start, end)
            job.count(fetched=len(outcome.data))
            job.note(
                provider=outcome.provider,
                degraded=outcome.degraded,
                attempts=[
                    {"provider": a.provider, "ok": a.ok, "detail": a.detail}
                    for a in outcome.attempts
                ],
            )
            if outcome.provider is None:
                job.note(reason="no provider could serve this league")
                return {"fetched": 0, "upserted": 0, "unresolved": 0}

            await store_raw(
                self.session,
                outcome.provider,
                f"fixtures/{league_slug}",
                {"start": start.isoformat(), "end": end.isoformat()},
                [f.model_dump(mode="json") for f in outcome.data],
            )

            upserted = unresolved = 0
            for raw in outcome.data:
                created = await self._upsert_fixture(raw, league, sport.id, outcome.provider)
                if created is None:
                    unresolved += 1
                else:
                    upserted += 1

            job.count(upserted=upserted)
            job.note(unresolved=unresolved)
            await self.session.commit()
            return {"fetched": len(outcome.data), "upserted": upserted, "unresolved": unresolved}

    async def ingest_results(self, league_slug: str, start: date, end: date) -> dict[str, int]:
        league = await self._league(league_slug)

        async with track_job(self.session, f"results:{league_slug}") as job:
            outcome = await self.registry.fetch_results(league_slug, start, end)
            job.count(fetched=len(outcome.data))
            job.note(provider=outcome.provider, degraded=outcome.degraded)
            if outcome.provider is None:
                return {"fetched": 0, "upserted": 0}

            await store_raw(
                self.session,
                outcome.provider,
                f"results/{league_slug}",
                {"start": start.isoformat(), "end": end.isoformat()},
                [r.model_dump(mode="json") for r in outcome.data],
            )

            upserted = 0
            for raw in outcome.data:
                if await self._upsert_result(raw, league.id, outcome.provider):
                    upserted += 1

            job.count(upserted=upserted)
            await self.session.commit()
            return {"fetched": len(outcome.data), "upserted": upserted}

    # -- internals -----------------------------------------------------------

    async def _league(self, slug: str) -> League:
        league = (
            await self.session.execute(select(League).where(League.slug == slug))
        ).scalar_one_or_none()
        if league is None:
            raise ValueError(f"unknown league '{slug}' — is it seeded in leagues.yaml?")
        return league

    async def _upsert_fixture(
        self, raw: RawFixture, league: League, sport_id: int, provider: str
    ) -> Fixture | None:
        home_id = await self._team_id(
            raw.home.name, raw.home.external_id, sport_id, provider, league.id
        )
        away_id = await self._team_id(
            raw.away.name, raw.away.external_id, sport_id, provider, league.id
        )
        if home_id is None or away_id is None:
            return None  # queued for review; never guess a participant

        fixture = await self._existing_fixture(raw.external_id, provider, league.id)
        if fixture is None:
            fixture = Fixture(sport_id=sport_id, league_id=league.id, start_time=raw.start_time)
            self.session.add(fixture)
            await self.session.flush()
            self.session.add_all(
                [
                    FixtureParticipant(fixture_id=fixture.id, side=Side.HOME, team_id=home_id),
                    FixtureParticipant(fixture_id=fixture.id, side=Side.AWAY, team_id=away_id),
                ]
            )
            await self._save_fixture_alias(fixture.id, raw.external_id, provider, sport_id)

        fixture.start_time = raw.start_time
        fixture.status = _status(raw.status)
        fixture.round = raw.round or fixture.round
        fixture.event_name = raw.event_name or fixture.event_name
        await self.session.flush()
        return fixture

    async def _upsert_result(self, raw: RawResult, league_id: int, provider: str) -> bool:
        fixture = await self._existing_fixture(raw.external_id, provider, league_id)
        if fixture is None:
            return False  # result for a fixture we never ingested

        result = (
            await self.session.execute(select(Result).where(Result.fixture_id == fixture.id))
        ).scalar_one_or_none()
        if result is None:
            result = Result(fixture_id=fixture.id)
            self.session.add(result)

        result.home_score = raw.home_score
        result.away_score = raw.away_score
        result.score_detail = raw.detail
        if raw.home_score is not None and raw.away_score is not None:
            if raw.home_score > raw.away_score:
                result.winner_side = Side.HOME
            elif raw.away_score > raw.home_score:
                result.winner_side = Side.AWAY
            else:
                result.winner_side = None  # draw
        if raw.status == "finished":
            result.finalized_at = datetime.now(UTC)
            fixture.status = FixtureStatus.FINISHED
        await self.session.flush()
        return True

    async def _team_id(
        self, name: str, external_id: str | None, sport_id: int, provider: str, league_id: int
    ) -> int | None:
        resolution = await self.resolver.resolve_team(
            provider, name, sport_id, external_id=external_id, league_id=league_id
        )
        if resolution.resolved:
            return resolution.entity_id

        # Nothing in the database resembles this name, so there is nothing it
        # could be confused with: this is a team we have not seen before
        # (season one, a promoted club, a newly covered league). Creating it is
        # not a guess. When candidates *do* exist the name is ambiguous, and it
        # stays in the review queue for a human.
        if resolution.candidate_count == 0:
            team = Team(sport_id=sport_id, name=name, normalized_name=normalize_name(name))
            self.session.add(team)
            await self.session.flush()
            # Resolve again so the alias is learned from the now-existing team.
            await self.resolver.resolve_team(
                provider, name, sport_id, external_id=external_id, league_id=league_id
            )
            # Close the queue item the first attempt opened. Without this, every
            # team ever auto-created leaves a pending entry behind, and the one
            # screen meant for genuine ambiguity fills with hundreds of rows
            # that need no decision — which is the same as having no queue.
            if resolution.queue_item_id is not None:
                await self.resolver.close_auto_created(resolution.queue_item_id, team.id)
            return team.id
        return None

    async def _existing_fixture(
        self, external_id: str, provider: str, league_id: int
    ) -> Fixture | None:
        alias = (
            await self.session.execute(
                select(EntityAlias).where(
                    EntityAlias.entity_type == EntityType.FIXTURE,
                    EntityAlias.provider == provider,
                    EntityAlias.external_id == external_id,
                )
            )
        ).scalar_one_or_none()
        if alias is None:
            return None
        return await self.session.get(Fixture, alias.entity_id)

    async def _save_fixture_alias(
        self, fixture_id: int, external_id: str, provider: str, sport_id: int
    ) -> None:
        from app.db.enums import AliasSource

        self.session.add(
            EntityAlias(
                entity_type=EntityType.FIXTURE,
                entity_id=fixture_id,
                sport_id=sport_id,
                provider=provider,
                external_id=external_id,
                source=AliasSource.PROVIDER_ID,
                confidence=1.0,
            )
        )
        await self.session.flush()


def _status(raw_status: str) -> FixtureStatus:
    try:
        return FixtureStatus(raw_status)
    except ValueError:
        return FixtureStatus.UNKNOWN
