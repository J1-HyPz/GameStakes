"""Provider interface and the normalised payloads every adapter returns.

Business logic never talks to a third-party API directly: it asks the registry
for a provider and receives these Raw* models. Adapters own every quirk of
their upstream — endpoint shapes, name spellings, pagination, auth — so a
provider can be swapped or fail without anything downstream noticing.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import ProviderError


class Market(enum.StrEnum):
    """Betting markets, mapped per-provider to their own keys."""

    MATCH_WINNER = "1x2"  # home/draw/away (moneyline where no draw exists)
    TOTALS = "totals"
    SPREADS = "spreads"
    BOTH_TEAMS_TO_SCORE = "btts"
    DOUBLE_CHANCE = "double_chance"
    CORRECT_SCORE = "correct_score"
    PLAYER_PROPS = "player_props"
    METHOD_OF_VICTORY = "method"
    TOTAL_ROUNDS = "rounds"


class ProviderCapability(enum.StrEnum):
    FIXTURES = "fixtures"
    RESULTS = "results"
    TEAM_STATS = "team_stats"
    PLAYER_STATS = "player_stats"
    ODDS = "odds"


class ProviderState(enum.StrEnum):
    UP = "up"
    DEGRADED = "degraded"  # reachable but rate-limited or partially failing
    DOWN = "down"
    DISABLED = "disabled"  # no API key configured


class ProviderHealth(BaseModel):
    name: str
    state: ProviderState
    detail: str | None = None
    # Remaining quota where the upstream reports it (The Odds API credits).
    quota_remaining: int | None = None
    checked_at: datetime | None = None


class RawParticipant(BaseModel):
    """One side of a fixture, exactly as the provider names it."""

    name: str
    external_id: str | None = None
    is_home: bool = True


class RawFixture(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_id: str
    league_code: str
    start_time: datetime
    status: str
    home: RawParticipant
    away: RawParticipant
    season: str | None = None
    round: str | None = None
    event_name: str | None = None  # combat-sport card
    venue: str | None = None
    scheduled_rounds: int | None = None


class RawResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_id: str
    league_code: str
    status: str
    home_score: int | None = None
    away_score: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class RawTeamStat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    team_name: str
    league_code: str
    season: str
    external_id: str | None = None
    played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    score_for: float | None = None
    score_against: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RawPlayerStat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    player_name: str
    league_code: str
    season: str
    team_name: str | None = None
    external_id: str | None = None
    fixture_external_id: str | None = None
    minutes: float | None = None
    stats: dict[str, Any] = Field(default_factory=dict)


class RawOdds(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fixture_external_id: str
    bookmaker: str
    market: str
    selection: str
    price_decimal: float
    line: float | None = None
    player_name: str | None = None
    captured_at: datetime


class UnsupportedCapability(ProviderError):
    """The provider does not offer this kind of data."""


@runtime_checkable
class SportsDataProvider(Protocol):
    """What every adapter implements. Methods a provider cannot serve raise
    UnsupportedCapability, which the registry treats as "try the next one"."""

    name: str
    supported_sports: set[str]
    supported_leagues: set[str]
    capabilities: set[ProviderCapability]

    def is_configured(self) -> bool: ...

    async def fetch_fixtures(self, league: str, start: date, end: date) -> list[RawFixture]: ...

    async def fetch_results(self, league: str, start: date, end: date) -> list[RawResult]: ...

    async def fetch_team_stats(self, league: str, season: str) -> list[RawTeamStat]: ...

    async def fetch_player_stats(self, league: str, season: str) -> list[RawPlayerStat]: ...

    async def fetch_odds(self, league: str, markets: list[Market]) -> list[RawOdds]: ...

    async def health(self) -> ProviderHealth: ...


class BaseProvider:
    """Shared defaults: everything unsupported until an adapter overrides it."""

    name: str = "base"
    supported_sports: set[str] = set()
    supported_leagues: set[str] = set()
    capabilities: set[ProviderCapability] = set()

    def is_configured(self) -> bool:
        return True

    def supports(self, league: str, capability: ProviderCapability) -> bool:
        return (
            self.is_configured()
            and capability in self.capabilities
            and league in self.supported_leagues
        )

    async def fetch_fixtures(self, league: str, start: date, end: date) -> list[RawFixture]:
        raise UnsupportedCapability(f"{self.name} does not provide fixtures")

    async def fetch_results(self, league: str, start: date, end: date) -> list[RawResult]:
        raise UnsupportedCapability(f"{self.name} does not provide results")

    async def fetch_team_stats(self, league: str, season: str) -> list[RawTeamStat]:
        raise UnsupportedCapability(f"{self.name} does not provide team stats")

    async def fetch_player_stats(self, league: str, season: str) -> list[RawPlayerStat]:
        raise UnsupportedCapability(f"{self.name} does not provide player stats")

    async def fetch_odds(self, league: str, markets: list[Market]) -> list[RawOdds]:
        raise UnsupportedCapability(f"{self.name} does not provide odds")

    async def health(self) -> ProviderHealth:
        raise NotImplementedError
