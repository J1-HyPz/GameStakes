"""Odds ingestion.

Snapshots are append-only: closing line value needs the price history, not the
latest price. Capture cadence is deliberately sparse — on The Odds API's free
tier a single greedy poll across markets can burn a day's credits — so the
scheduler takes an opening reading, a mid-window one, and one close to kickoff,
and the tracker treats the last snapshot before kickoff as the closing line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import EntityType, FixtureStatus
from app.db.models import EntityAlias, Fixture, League, OddsSnapshot
from app.ingest.jobs import track_job
from app.providers.base import Market
from app.providers.registry import ProviderRegistry, get_registry

log = get_logger(__name__)

DEFAULT_MARKETS = [Market.MATCH_WINNER, Market.TOTALS]


class OddsIngestor:
    def __init__(self, session: AsyncSession, registry: ProviderRegistry | None = None):
        self.session = session
        self.registry = registry or get_registry()

    async def ingest_league(
        self, league_slug: str, markets: list[Market] | None = None
    ) -> dict[str, int]:
        markets = markets or DEFAULT_MARKETS
        league = (
            await self.session.execute(select(League).where(League.slug == league_slug))
        ).scalar_one_or_none()
        if league is None:
            raise ValueError(f"unknown league '{league_slug}'")

        async with track_job(self.session, f"odds:{league_slug}") as job:
            outcome = await self.registry.fetch_odds(league_slug, markets)
            job.count(fetched=len(outcome.data))
            job.note(
                provider=outcome.provider,
                degraded=outcome.degraded,
                markets=[m.value for m in markets],
            )
            if outcome.provider is None:
                job.note(reason="no configured provider supplies odds for this league")
                return {"fetched": 0, "stored": 0, "unmatched": 0}

            stored = unmatched = 0
            for raw in outcome.data:
                fixture_id = await self._fixture_id(raw.fixture_external_id, outcome.provider)
                if fixture_id is None:
                    unmatched += 1
                    continue
                self.session.add(
                    OddsSnapshot(
                        fixture_id=fixture_id,
                        bookmaker=raw.bookmaker,
                        market=raw.market,
                        selection=raw.selection,
                        line=Decimal(str(raw.line)) if raw.line is not None else None,
                        price_decimal=Decimal(str(raw.price_decimal)),
                        provider=outcome.provider,
                        captured_at=raw.captured_at,
                    )
                )
                stored += 1

            job.count(upserted=stored)
            job.note(unmatched=unmatched)
            await self.session.commit()
            return {"fetched": len(outcome.data), "stored": stored, "unmatched": unmatched}

    async def _fixture_id(self, external_id: str, provider: str) -> int | None:
        alias = (
            await self.session.execute(
                select(EntityAlias).where(
                    EntityAlias.entity_type == EntityType.FIXTURE,
                    EntityAlias.provider == provider,
                    EntityAlias.external_id == external_id,
                )
            )
        ).scalar_one_or_none()
        return alias.entity_id if alias else None


async def latest_odds(
    session: AsyncSession, fixture_id: int
) -> dict[tuple[str, str, Decimal | None], list[OddsSnapshot]]:
    """Most recent price per (market, selection, line) per bookmaker."""
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

    latest: dict[tuple[str, str, Decimal | None], dict[str, OddsSnapshot]] = {}
    for snapshot in snapshots:
        key = (snapshot.market, snapshot.selection, snapshot.line)
        per_book = latest.setdefault(key, {})
        if snapshot.bookmaker not in per_book:  # first seen is newest
            per_book[snapshot.bookmaker] = snapshot
    return {key: list(books.values()) for key, books in latest.items()}


async def closing_line(
    session: AsyncSession,
    fixture_id: int,
    market: str,
    selection: str,
    line: Decimal | None,
) -> OddsSnapshot | None:
    """The last price captured before kickoff.

    On a metered free tier this is "the last snapshot we could afford", not a
    true closing line — the tracker labels it as such rather than implying more
    precision than the data supports.
    """
    fixture = await session.get(Fixture, fixture_id)
    if fixture is None:
        return None

    stmt = (
        select(OddsSnapshot)
        .where(
            OddsSnapshot.fixture_id == fixture_id,
            OddsSnapshot.market == market,
            OddsSnapshot.selection == selection,
            OddsSnapshot.captured_at <= fixture.start_time,
        )
        .order_by(OddsSnapshot.captured_at.desc())
        .limit(1)
    )
    if line is None:
        stmt = stmt.where(OddsSnapshot.line.is_(None))
    else:
        stmt = stmt.where(OddsSnapshot.line == line)
    return (await session.execute(stmt)).scalars().first()


async def ingest_odds_for_upcoming(session: AsyncSession, within_hours: int = 48) -> dict[str, int]:
    """Refresh odds for leagues with fixtures kicking off soon.

    Scoped to imminent fixtures because credits are finite: prices a fortnight
    out move enough to be worth little and cost the same as prices tonight.
    """
    now = datetime.now(UTC)
    stmt = (
        select(League.slug)
        .join(Fixture, Fixture.league_id == League.id)
        .where(
            Fixture.start_time >= now,
            Fixture.start_time <= now + timedelta(hours=within_hours),
            Fixture.status == FixtureStatus.SCHEDULED,
        )
        .distinct()
    )
    slugs = list((await session.execute(stmt)).scalars().all())

    ingestor = OddsIngestor(session)
    totals = {"leagues": len(slugs), "fetched": 0, "stored": 0}
    for slug in slugs:
        try:
            counts = await ingestor.ingest_league(slug)
            totals["fetched"] += counts["fetched"]
            totals["stored"] += counts["stored"]
        except Exception:  # noqa: BLE001 — one league must not stop the rest
            log.exception("odds.league_failed", league=slug)
    return totals
