"""Entity resolution: mapping provider names/ids to canonical entities.

Providers spell the same team or player differently ("Man Utd", "Manchester
United", "Manchester Utd FC"). Resolution runs through a ladder, cheapest
first:

1. provider external id  -> existing alias (exact, stable)
2. normalized alias name -> existing alias for this provider
3. normalized exact match against canonical names (any provider taught us)
4. fuzzy match (rapidfuzz) with an auto-accept threshold AND a margin over the
   runner-up — a high score that is nearly tied with second place is exactly
   the case a human must decide
5. otherwise: queue for the manual-override admin screen. Never guess.

Every successful step writes an alias so the next lookup is O(1).
"""

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import AliasSource, EntityType, ResolutionStatus
from app.db.models import EntityAlias, Player, ResolutionQueueItem, Team

log = get_logger(__name__)

# Auto-accept a fuzzy match only at/above this score...
AUTO_ACCEPT_SCORE = 92.0
# ...and only when the runner-up trails by at least this much. Near-ties
# (e.g. "United Reds" vs "United Red") must go to a human.
AMBIGUITY_MARGIN = 5.0
# Below this, a candidate is not even worth listing for the human.
CANDIDATE_FLOOR = 60.0
MAX_CANDIDATES = 5

# Noise tokens stripped during normalisation. Deliberately conservative:
# only legal-form suffixes that never disambiguate two clubs.
_STOP_TOKENS = {"fc", "cf", "afc", "cfc", "ssc", "ac", "as", "sc", "bk", "if", "sv"}


def normalize_name(name: str) -> str:
    """Casefold, strip accents and punctuation, drop legal-form suffix tokens,
    collapse whitespace. 'Atlético de Madrid' -> 'atletico de madrid'."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    # Periods and apostrophes join ("F.C." -> "fc", "D'Angelo" -> "dangelo");
    # remaining punctuation splits.
    joined = ascii_only.replace(".", "").replace("'", "")
    cleaned = "".join(c if c.isalnum() else " " for c in joined.casefold())
    tokens = [t for t in cleaned.split() if t not in _STOP_TOKENS]
    return " ".join(tokens) if tokens else " ".join(cleaned.split())


def _has_extra_tokens(query: str, candidate: str) -> bool:
    """True when one name carries whole tokens the other lacks.

    A trailing modifier is nearly free under token_sort_ratio, so
    "Manchester United W" (the women's team) scores 94 against "Manchester
    United" and would otherwise auto-accept as the men's club — permanently,
    via the learned alias. The same holds for "B", "II", "U21" and reserve
    sides. Spelling variants are unaffected: "moenchengladbach" vs
    "monchengladbach" each hold a token the other lacks, so neither is a
    superset of the other.
    """
    query_tokens, candidate_tokens = set(query.split()), set(candidate.split())
    return bool(query_tokens ^ candidate_tokens) and (
        query_tokens <= candidate_tokens or candidate_tokens <= query_tokens
    )


class InvalidResolutionError(ValueError):
    """A manual resolution names an entity that does not exist or does not fit
    the queue item (wrong sport, wrong entity type)."""


@dataclass(frozen=True)
class Resolution:
    """Outcome of a resolution attempt."""

    entity_id: int | None
    queued: bool = False
    queue_item_id: int | None = None
    source: AliasSource | None = None
    score: float | None = None

    @property
    def resolved(self) -> bool:
        return self.entity_id is not None


class EntityResolver:
    """Resolves provider team/player references against canonical tables.

    Does not commit — the caller owns the transaction, so a whole ingest batch
    commits or rolls back together.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_team(
        self,
        provider: str,
        raw_name: str,
        sport_id: int,
        external_id: str | None = None,
        league_id: int | None = None,
    ) -> Resolution:
        return await self._resolve(
            EntityType.TEAM, provider, raw_name, sport_id, external_id, league_id
        )

    async def resolve_player(
        self,
        provider: str,
        raw_name: str,
        sport_id: int,
        external_id: str | None = None,
        league_id: int | None = None,
    ) -> Resolution:
        return await self._resolve(
            EntityType.PLAYER, provider, raw_name, sport_id, external_id, league_id
        )

    async def _resolve(
        self,
        entity_type: EntityType,
        provider: str,
        raw_name: str,
        sport_id: int,
        external_id: str | None,
        league_id: int | None,
    ) -> Resolution:
        normalized = normalize_name(raw_name)

        # 1. Provider external id — the strongest signal.
        if external_id is not None:
            alias = await self._alias_by_external_id(entity_type, provider, external_id)
            if alias is not None:
                return Resolution(alias.entity_id, source=AliasSource.PROVIDER_ID)

        # A fully non-Latin name (Cyrillic, Greek, CJK) normalises to nothing.
        # Name-based matching is meaningless here and an empty alias would act
        # as a catch-all that swallows every later non-Latin name, so go
        # straight to the human queue.
        if not normalized:
            item = await self._enqueue(
                entity_type,
                provider,
                raw_name,
                sport_id,
                league_id,
                external_id,
                candidates=[],
                context={"reason": "name has no Latin characters to match on"},
            )
            return Resolution(None, queued=True, queue_item_id=item.id)

        # 2. Known alias name for this provider.
        alias = await self._alias_by_name(entity_type, provider, sport_id, normalized)
        if alias is not None:
            # Learn the external id for next time if we just gained one.
            if external_id is not None and alias.external_id is None:
                alias.external_id = external_id
            return Resolution(alias.entity_id, source=alias.source)

        # 3. Exact normalized match against canonical names.
        canonical = await self._canonical_names(entity_type, sport_id)
        exact_ids = [eid for eid, name in canonical.items() if name == normalized]
        if len(exact_ids) == 1:
            await self._save_alias(
                entity_type,
                exact_ids[0],
                provider,
                raw_name,
                normalized,
                external_id,
                sport_id,
                AliasSource.EXACT,
                confidence=1.0,
            )
            return Resolution(exact_ids[0], source=AliasSource.EXACT, score=100.0)

        # 4. Fuzzy match with threshold + margin.
        if canonical and len(exact_ids) == 0:
            matches = process.extract(
                normalized,
                canonical,
                scorer=fuzz.token_sort_ratio,
                limit=MAX_CANDIDATES,
                score_cutoff=CANDIDATE_FLOOR,
            )
            if matches:
                best_name, best_score, best_id = matches[0]
                runner_up = matches[1][1] if len(matches) > 1 else 0.0
                if (
                    best_score >= AUTO_ACCEPT_SCORE
                    and best_score - runner_up >= AMBIGUITY_MARGIN
                    and not _has_extra_tokens(normalized, best_name)
                ):
                    await self._save_alias(
                        entity_type,
                        best_id,
                        provider,
                        raw_name,
                        normalized,
                        external_id,
                        sport_id,
                        AliasSource.FUZZY,
                        confidence=best_score / 100.0,
                    )
                    log.info(
                        "resolution.fuzzy_accept",
                        raw_name=raw_name,
                        matched=best_name,
                        score=best_score,
                        provider=provider,
                    )
                    return Resolution(best_id, source=AliasSource.FUZZY, score=best_score)

                item = await self._enqueue(
                    entity_type,
                    provider,
                    raw_name,
                    sport_id,
                    league_id,
                    external_id,
                    candidates=[
                        {"entity_id": eid, "name": name, "score": round(score, 1)}
                        for name, score, eid in matches
                    ],
                )
                return Resolution(None, queued=True, queue_item_id=item.id)

        # Ambiguous exact duplicates, or nothing remotely close: human decides.
        item = await self._enqueue(
            entity_type,
            provider,
            raw_name,
            sport_id,
            league_id,
            external_id,
            candidates=[
                {"entity_id": eid, "name": canonical[eid], "score": 100.0} for eid in exact_ids
            ],
        )
        return Resolution(None, queued=True, queue_item_id=item.id)

    async def resolve_manually(self, queue_item_id: int, entity_id: int) -> ResolutionQueueItem:
        """Apply a human decision from the admin screen: create a MANUAL alias
        and close the queue item.

        The chosen entity is validated first — entity_aliases carries no
        foreign key, so an unchecked id would create an alias pointing at
        nothing and silently answer every future lookup for that name.
        """
        item = await self.session.get(ResolutionQueueItem, queue_item_id)
        if item is None:
            raise ValueError(f"resolution queue item {queue_item_id} not found")

        # sport_id is non-nullable on both models, so None means "no such row".
        if item.entity_type == EntityType.TEAM:
            entity_sport_id = await self.session.scalar(
                select(Team.sport_id).where(Team.id == entity_id)
            )
        elif item.entity_type == EntityType.PLAYER:
            entity_sport_id = await self.session.scalar(
                select(Player.sport_id).where(Player.id == entity_id)
            )
        else:
            raise InvalidResolutionError(
                f"manual resolution is not supported for {item.entity_type.value}"
            )

        if entity_sport_id is None:
            raise InvalidResolutionError(f"{item.entity_type.value} {entity_id} does not exist")
        if item.sport_id is not None and entity_sport_id != item.sport_id:
            raise InvalidResolutionError(
                f"{item.entity_type.value} {entity_id} belongs to sport "
                f"{entity_sport_id}, but this queue item is sport {item.sport_id}"
            )

        # A name with no Latin characters normalises to nothing; store NULL
        # rather than "" so it cannot become a catch-all alias, and rely on the
        # external id for future lookups.
        normalized = normalize_name(item.raw_name) or None
        if normalized is not None or item.external_id is not None:
            await self._save_alias(
                item.entity_type,
                entity_id,
                item.provider,
                item.raw_name,
                normalized,
                item.external_id,
                item.sport_id,
                AliasSource.MANUAL,
                confidence=1.0,
            )
        item.status = ResolutionStatus.RESOLVED
        item.resolved_entity_id = entity_id
        item.resolved_at = datetime.now(UTC)
        return item

    async def ignore(self, queue_item_id: int) -> ResolutionQueueItem:
        item = await self.session.get(ResolutionQueueItem, queue_item_id)
        if item is None:
            raise ValueError(f"resolution queue item {queue_item_id} not found")
        item.status = ResolutionStatus.IGNORED
        item.resolved_at = datetime.now(UTC)
        return item

    # -- internals -----------------------------------------------------------

    async def _alias_by_external_id(
        self, entity_type: EntityType, provider: str, external_id: str
    ) -> EntityAlias | None:
        stmt = select(EntityAlias).where(
            EntityAlias.entity_type == entity_type,
            EntityAlias.provider == provider,
            EntityAlias.external_id == external_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _alias_by_name(
        self, entity_type: EntityType, provider: str, sport_id: int, normalized: str
    ) -> EntityAlias | None:
        stmt = select(EntityAlias).where(
            EntityAlias.entity_type == entity_type,
            EntityAlias.provider == provider,
            EntityAlias.sport_id == sport_id,
            EntityAlias.normalized_alias == normalized,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _canonical_names(self, entity_type: EntityType, sport_id: int) -> dict[int, str]:
        if entity_type == EntityType.TEAM:
            stmt = select(Team.id, Team.normalized_name).where(Team.sport_id == sport_id)
        else:
            stmt = select(Player.id, Player.normalized_name).where(Player.sport_id == sport_id)
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def _save_alias(
        self,
        entity_type: EntityType,
        entity_id: int,
        provider: str,
        raw_name: str,
        normalized: str | None,
        external_id: str | None,
        sport_id: int | None,
        source: AliasSource,
        confidence: float,
    ) -> EntityAlias:
        alias = EntityAlias(
            entity_type=entity_type,
            entity_id=entity_id,
            sport_id=sport_id,
            provider=provider,
            alias_name=raw_name,
            normalized_alias=normalized,
            external_id=external_id,
            source=source,
            confidence=confidence,
        )
        self.session.add(alias)
        await self.session.flush()
        return alias

    async def _enqueue(
        self,
        entity_type: EntityType,
        provider: str,
        raw_name: str,
        sport_id: int,
        league_id: int | None,
        external_id: str | None,
        candidates: list[dict[str, object]],
        context: dict[str, object] | None = None,
    ) -> ResolutionQueueItem:
        # Reuse an open item for the same unresolved name — no queue spam.
        stmt = select(ResolutionQueueItem).where(
            ResolutionQueueItem.entity_type == entity_type,
            ResolutionQueueItem.provider == provider,
            ResolutionQueueItem.raw_name == raw_name,
            ResolutionQueueItem.sport_id == sport_id,
            ResolutionQueueItem.status == ResolutionStatus.PENDING,
        )
        existing = (await self.session.execute(stmt)).scalars().first()
        if existing is not None:
            existing.candidates = candidates  # refresh with latest scores
            return existing

        item = ResolutionQueueItem(
            entity_type=entity_type,
            provider=provider,
            raw_name=raw_name,
            sport_id=sport_id,
            league_id=league_id,
            external_id=external_id,
            candidates=candidates,
            context=context or {},
        )
        self.session.add(item)
        await self.session.flush()
        log.info(
            "resolution.queued",
            entity_type=entity_type.value,
            raw_name=raw_name,
            provider=provider,
            n_candidates=len(candidates),
        )
        return item
