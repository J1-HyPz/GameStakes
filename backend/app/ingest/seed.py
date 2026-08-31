"""Seed sports and leagues from the YAML catalogue.

Idempotent: rows are matched by slug and updated in place, so re-running (the
container does, on every start) never duplicates and picks up YAML edits.
Leagues removed from the YAML are deactivated, never deleted — history stays.

Run manually:  python -m app.ingest.seed
"""

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging, get_logger
from app.db.enums import CompetitionType, SportKind
from app.db.models import League, Sport

SEED_FILE = Path(__file__).parent / "seeds" / "leagues.yaml"

log = get_logger(__name__)


def load_seed_config(path: Path = SEED_FILE) -> dict[str, Any]:
    with path.open() as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


async def seed_catalog(
    session: AsyncSession, config: dict[str, Any] | None = None
) -> dict[str, int]:
    """Upsert sports and leagues; returns counts for job reporting."""
    cfg = config if config is not None else load_seed_config()
    counts = {"sports": 0, "leagues": 0, "deactivated": 0}

    existing_sports = {s.slug: s for s in (await session.execute(select(Sport))).scalars()}
    existing_leagues = {le.slug: le for le in (await session.execute(select(League))).scalars()}
    seen_league_slugs: set[str] = set()

    for sport_cfg in cfg["sports"]:
        sport = existing_sports.get(sport_cfg["slug"])
        if sport is None:
            sport = Sport(slug=sport_cfg["slug"])
            session.add(sport)
        sport.name = sport_cfg["name"]
        sport.kind = SportKind(sport_cfg["kind"])
        await session.flush()  # sport.id for new rows
        counts["sports"] += 1

        for league_cfg in sport_cfg.get("leagues", []):
            slug = league_cfg["slug"]
            seen_league_slugs.add(slug)
            league = existing_leagues.get(slug)
            if league is None:
                league = League(slug=slug, sport_id=sport.id)
                session.add(league)
            league.sport_id = sport.id
            league.name = league_cfg["name"]
            league.short_name = league_cfg.get("short_name")
            league.country = league_cfg.get("country")
            league.competition_type = CompetitionType(league_cfg["competition_type"])
            league.tier = league_cfg.get("tier")
            league.is_active = True
            counts["leagues"] += 1

    for slug, league in existing_leagues.items():
        if slug not in seen_league_slugs and league.is_active:
            league.is_active = False
            counts["deactivated"] += 1

    await session.commit()
    return counts


async def _amain() -> None:
    from app.core.config import get_settings
    from app.db.session import get_sessionmaker

    settings = get_settings()
    configure_logging(settings.log_level, settings.environment)
    async with get_sessionmaker()() as session:
        counts = await seed_catalog(session)
    log.info("seed.complete", **counts)


if __name__ == "__main__":
    import asyncio

    asyncio.run(_amain())
