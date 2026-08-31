"""League seeding: idempotency and deactivation semantics."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import League, Sport
from app.ingest.seed import load_seed_config, seed_catalog


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    before = (await db_session.execute(select(func.count(League.id)))).scalar_one()
    counts = await seed_catalog(db_session)
    after = (await db_session.execute(select(func.count(League.id)))).scalar_one()

    assert before == after  # no duplicates on re-run
    assert counts["sports"] == 5
    assert counts["leagues"] == 35


async def test_seed_covers_all_configured_sports(db_session: AsyncSession) -> None:
    slugs = set((await db_session.execute(select(Sport.slug))).scalars())
    assert slugs == {"football", "american-football", "basketball", "boxing", "mma"}


async def test_removed_league_is_deactivated_not_deleted(db_session: AsyncSession) -> None:
    config = load_seed_config()
    football = next(s for s in config["sports"] if s["slug"] == "football")
    removed = football["leagues"].pop()

    counts = await seed_catalog(db_session, config)
    assert counts["deactivated"] == 1

    league = (
        await db_session.execute(select(League).where(League.slug == removed["slug"]))
    ).scalar_one()
    assert league.is_active is False

    # Restoring the config reactivates it.
    football["leagues"].append(removed)
    await seed_catalog(db_session, config)
    await db_session.refresh(league)
    assert league.is_active is True
