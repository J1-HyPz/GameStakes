"""Entity resolution: normalisation, the resolution ladder, and the manual queue."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import AliasSource, EntityType, ResolutionStatus
from app.db.models import EntityAlias, ResolutionQueueItem, Sport, Team
from app.ingest.resolution import (
    EntityResolver,
    InvalidResolutionError,
    normalize_name,
)


class TestNormalizeName:
    def test_strips_accents(self) -> None:
        assert normalize_name("Atlético de Madrid") == "atletico de madrid"

    def test_strips_legal_suffixes_and_punctuation(self) -> None:
        assert normalize_name("Liverpool F.C.") == "liverpool"
        assert normalize_name("AFC Bournemouth") == "bournemouth"

    def test_casefolds_and_collapses_whitespace(self) -> None:
        assert normalize_name("  MANCHESTER   UNITED ") == "manchester united"

    def test_all_stop_tokens_falls_back_to_cleaned(self) -> None:
        # A name made only of stop tokens must not normalise to empty.
        assert normalize_name("FC") == "fc"


@pytest.fixture
async def football_sport_id(db_session: AsyncSession) -> int:
    sport = (await db_session.execute(select(Sport).where(Sport.slug == "football"))).scalar_one()
    return sport.id


async def _make_team(session: AsyncSession, sport_id: int, name: str) -> Team:
    team = Team(sport_id=sport_id, name=name, normalized_name=normalize_name(name))
    session.add(team)
    await session.flush()
    return team


async def test_exact_match_creates_alias(db_session: AsyncSession, football_sport_id: int) -> None:
    team = await _make_team(db_session, football_sport_id, "Wolverhampton Wanderers")
    resolver = EntityResolver(db_session)

    result = await resolver.resolve_team(
        "prov-exact", "Wolverhampton Wanderers FC", football_sport_id
    )

    assert result.resolved and result.entity_id == team.id
    assert result.source == AliasSource.EXACT
    alias = (
        await db_session.execute(
            select(EntityAlias).where(
                EntityAlias.provider == "prov-exact",
                EntityAlias.entity_id == team.id,
            )
        )
    ).scalar_one()
    assert alias.normalized_alias == "wolverhampton wanderers"


async def test_external_id_wins_over_everything(
    db_session: AsyncSession, football_sport_id: int
) -> None:
    team = await _make_team(db_session, football_sport_id, "Sporting CP")
    db_session.add(
        EntityAlias(
            entity_type=EntityType.TEAM,
            entity_id=team.id,
            sport_id=football_sport_id,
            provider="prov-ext",
            external_id="team-988",
            source=AliasSource.MANUAL,
            confidence=1.0,
        )
    )
    await db_session.flush()
    resolver = EntityResolver(db_session)

    # Wildly different name, but the external id is known.
    result = await resolver.resolve_team(
        "prov-ext", "Completely Different Name", football_sport_id, external_id="team-988"
    )
    assert result.entity_id == team.id
    assert result.source == AliasSource.PROVIDER_ID


async def test_fuzzy_accepts_clear_match(db_session: AsyncSession, football_sport_id: int) -> None:
    team = await _make_team(db_session, football_sport_id, "Borussia Mönchengladbach")
    resolver = EntityResolver(db_session)

    result = await resolver.resolve_team(
        "prov-fuzzy", "Borussia Moenchengladbach", football_sport_id
    )
    assert result.resolved and result.entity_id == team.id
    assert result.source == AliasSource.FUZZY

    # Second lookup hits the learned alias, not fuzzy again.
    again = await resolver.resolve_team(
        "prov-fuzzy", "Borussia Moenchengladbach", football_sport_id
    )
    assert again.entity_id == team.id
    assert again.source == AliasSource.FUZZY  # stored source on the alias


async def test_ambiguous_match_is_queued_not_guessed(
    db_session: AsyncSession, football_sport_id: int
) -> None:
    await _make_team(db_session, football_sport_id, "FC United Reds")
    await _make_team(db_session, football_sport_id, "FC United Red")
    resolver = EntityResolver(db_session)

    result = await resolver.resolve_team("prov-ambig", "FC United Redz", football_sport_id)

    assert not result.resolved and result.queued
    item = await db_session.get(ResolutionQueueItem, result.queue_item_id)
    assert item is not None and item.status == ResolutionStatus.PENDING
    assert len(item.candidates) >= 2


async def test_no_match_queues_with_no_strong_candidates(
    db_session: AsyncSession, football_sport_id: int
) -> None:
    resolver = EntityResolver(db_session)
    result = await resolver.resolve_team("prov-none", "Zzyzx Quasar Wanderers", football_sport_id)
    assert not result.resolved and result.queued


async def test_requeue_reuses_open_item(db_session: AsyncSession, football_sport_id: int) -> None:
    resolver = EntityResolver(db_session)
    first = await resolver.resolve_team("prov-dup", "Nonexistent XI", football_sport_id)
    second = await resolver.resolve_team("prov-dup", "Nonexistent XI", football_sport_id)
    assert first.queue_item_id == second.queue_item_id


async def test_manual_resolution_teaches_alias(
    db_session: AsyncSession, football_sport_id: int
) -> None:
    team = await _make_team(db_session, football_sport_id, "Internazionale")
    resolver = EntityResolver(db_session)

    queued = await resolver.resolve_team("prov-manual", "Inter", football_sport_id)
    assert queued.queued and queued.queue_item_id is not None

    item = await resolver.resolve_manually(queued.queue_item_id, team.id)
    assert item.status == ResolutionStatus.RESOLVED
    assert item.resolved_entity_id == team.id

    # The lesson sticks: next time resolves instantly via the alias.
    result = await resolver.resolve_team("prov-manual", "Inter", football_sport_id)
    assert result.entity_id == team.id
    assert result.source == AliasSource.MANUAL


async def test_ignore_closes_item(db_session: AsyncSession, football_sport_id: int) -> None:
    resolver = EntityResolver(db_session)
    queued = await resolver.resolve_team("prov-ignore", "Some Reserve Team", football_sport_id)
    assert queued.queue_item_id is not None
    item = await resolver.ignore(queued.queue_item_id)
    assert item.status == ResolutionStatus.IGNORED


async def test_womens_and_b_teams_do_not_fuzzy_accept_the_first_team(
    db_session: AsyncSession, football_sport_id: int
) -> None:
    """A trailing modifier token is nearly free under token_sort_ratio, so
    these once auto-accepted the men's first team — permanently, via the alias."""
    for canonical in ("Manchester United", "Tottenham Hotspur", "Atletico Madrid"):
        await _make_team(db_session, football_sport_id, canonical)
    resolver = EntityResolver(db_session)

    for variant in ("Manchester United W", "Tottenham Hotspur W", "Atletico Madrid B"):
        result = await resolver.resolve_team("prov-modifier", variant, football_sport_id)
        assert not result.resolved, f"{variant!r} must not auto-accept"
        assert result.queued


async def test_non_latin_names_never_share_a_catch_all_alias(
    db_session: AsyncSession, football_sport_id: int
) -> None:
    """Names that normalise to nothing must queue individually, not collapse
    onto one empty alias that answers every later non-Latin lookup."""
    olympiacos = await _make_team(db_session, football_sport_id, "Olympiacos")
    resolver = EntityResolver(db_session)

    first = await resolver.resolve_team("prov-greek", "Ολυμπιακός", football_sport_id)
    assert first.queued and first.queue_item_id is not None
    await resolver.resolve_manually(first.queue_item_id, olympiacos.id)

    second = await resolver.resolve_team("prov-greek", "Παναθηναϊκός", football_sport_id)
    assert not second.resolved, "a different Greek name must not inherit the alias"
    assert second.queued


async def test_manual_resolution_rejects_unusable_entities(
    db_session: AsyncSession, football_sport_id: int
) -> None:
    resolver = EntityResolver(db_session)

    queued = await resolver.resolve_team("prov-validate", "Ghost Town FC", football_sport_id)
    assert queued.queue_item_id is not None

    with pytest.raises(InvalidResolutionError):
        await resolver.resolve_manually(queued.queue_item_id, 999_999)

    mma = (await db_session.execute(select(Sport).where(Sport.slug == "mma"))).scalar_one()
    wrong_sport = await _make_team(db_session, mma.id, "Wrong Sport Team")
    with pytest.raises(InvalidResolutionError):
        await resolver.resolve_manually(queued.queue_item_id, wrong_sport.id)

    aliases = (
        (
            await db_session.execute(
                select(EntityAlias).where(EntityAlias.provider == "prov-validate")
            )
        )
        .scalars()
        .all()
    )
    assert aliases == [], "a rejected resolution must not leave an alias behind"
