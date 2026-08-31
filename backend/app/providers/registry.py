"""Provider registry: priority ordering, failover, and honest degradation.

Callers ask for data by league and capability, never by provider name. The
registry tries providers in priority order and moves on when one is
unconfigured, doesn't cover the league, or fails — recording every attempt so
the UI can say *which* source was used and what fell over, rather than
silently serving thinner data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypeVar

from app.core.exceptions import ProviderError
from app.core.logging import get_logger
from app.providers.balldontlie import BallDontLieProvider
from app.providers.base import (
    BaseProvider,
    Market,
    ProviderCapability,
    ProviderHealth,
    RawFixture,
    RawOdds,
    RawPlayerStat,
    RawResult,
    RawTeamStat,
)
from app.providers.espn import EspnProvider
from app.providers.football_data import FootballDataProvider
from app.providers.the_odds_api import TheOddsApiProvider
from app.providers.thesportsdb import TheSportsDbProvider

log = get_logger(__name__)

T = TypeVar("T")


@dataclass
class Attempt:
    provider: str
    ok: bool
    detail: str | None = None


@dataclass
class FetchOutcome[T]:
    """What came back, who served it, and what went wrong on the way."""

    data: list[T]
    provider: str | None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return any(not a.ok for a in self.attempts)


class ProviderRegistry:
    """Ordered provider list. Earlier entries are preferred."""

    def __init__(self, providers: list[BaseProvider] | None = None):
        self.providers: list[BaseProvider] = (
            providers if providers is not None else default_providers()
        )

    def by_name(self, name: str) -> BaseProvider | None:
        return next((p for p in self.providers if p.name == name), None)

    def candidates(self, league: str, capability: ProviderCapability) -> list[BaseProvider]:
        return [p for p in self.providers if p.supports(league, capability)]

    def leagues_for(self, provider: BaseProvider) -> set[str]:
        return provider.supported_leagues if provider.is_configured() else set()

    async def health(self) -> list[ProviderHealth]:
        results = []
        for provider in self.providers:
            try:
                results.append(await provider.health())
            except Exception as exc:  # noqa: BLE001 — health never raises
                from app.providers.base import ProviderState

                results.append(
                    ProviderHealth(name=provider.name, state=ProviderState.DOWN, detail=str(exc))
                )
        return results

    async def _try_each[T](
        self,
        league: str,
        capability: ProviderCapability,
        call: Callable[[BaseProvider], Awaitable[list[T]]],
    ) -> FetchOutcome[T]:
        attempts: list[Attempt] = []
        candidates = self.candidates(league, capability)
        if not candidates:
            log.warning("provider.no_candidates", league=league, capability=capability.value)
            return FetchOutcome(data=[], provider=None, attempts=attempts)

        for provider in candidates:
            try:
                data = await call(provider)
            except (ProviderError, ValueError) as exc:
                attempts.append(Attempt(provider.name, ok=False, detail=str(exc)))
                log.warning(
                    "provider.failed",
                    provider=provider.name,
                    league=league,
                    capability=capability.value,
                    error=str(exc),
                )
                continue
            except Exception as exc:  # noqa: BLE001 — one bad adapter must not stop the rest
                attempts.append(Attempt(provider.name, ok=False, detail=repr(exc)))
                log.exception("provider.unexpected_error", provider=provider.name, league=league)
                continue

            attempts.append(Attempt(provider.name, ok=True))
            return FetchOutcome(data=data, provider=provider.name, attempts=attempts)

        return FetchOutcome(data=[], provider=None, attempts=attempts)

    async def fetch_fixtures(self, league: str, start: date, end: date) -> FetchOutcome[RawFixture]:
        return await self._try_each(
            league, ProviderCapability.FIXTURES, lambda p: p.fetch_fixtures(league, start, end)
        )

    async def fetch_results(self, league: str, start: date, end: date) -> FetchOutcome[RawResult]:
        return await self._try_each(
            league, ProviderCapability.RESULTS, lambda p: p.fetch_results(league, start, end)
        )

    async def fetch_team_stats(self, league: str, season: str) -> FetchOutcome[RawTeamStat]:
        return await self._try_each(
            league, ProviderCapability.TEAM_STATS, lambda p: p.fetch_team_stats(league, season)
        )

    async def fetch_player_stats(self, league: str, season: str) -> FetchOutcome[RawPlayerStat]:
        return await self._try_each(
            league,
            ProviderCapability.PLAYER_STATS,
            lambda p: p.fetch_player_stats(league, season),
        )

    async def fetch_odds(self, league: str, markets: list[Market]) -> FetchOutcome[RawOdds]:
        return await self._try_each(
            league, ProviderCapability.ODDS, lambda p: p.fetch_odds(league, markets)
        )

    async def aclose(self) -> None:
        for provider in self.providers:
            client = getattr(provider, "client", None)
            if client is not None:
                await client.aclose()


def default_providers() -> list[BaseProvider]:
    """Priority order: specialist and documented sources first, best-effort last.

    football-data.org leads for European football (clean, documented, free);
    balldontlie leads for the NBA; The Odds API is the only odds source and
    also supplies free event lists; TheSportsDB is the broad fallback and the
    only free boxing coverage; ESPN is last because it can break without
    notice.
    """
    return [
        FootballDataProvider(),
        BallDontLieProvider(),
        TheOddsApiProvider(),
        TheSportsDbProvider(),
        EspnProvider(),
    ]


_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


async def reset_registry() -> None:
    """Rebuild the registry — after a key changes in settings, and in tests."""
    global _registry
    if _registry is not None:
        await _registry.aclose()
    _registry = None


def describe_providers(registry: ProviderRegistry) -> list[dict[str, Any]]:
    """Static description for the settings page: no network calls."""
    return [
        {
            "name": p.name,
            "configured": p.is_configured(),
            "best_effort": getattr(p, "best_effort", False),
            "sports": sorted(p.supported_sports),
            "leagues": sorted(p.supported_leagues),
            "capabilities": sorted(c.value for c in p.capabilities),
            "priority": index,
        }
        for index, p in enumerate(registry.providers)
    ]
