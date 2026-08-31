"""TheSportsDB adapter — broad multi-sport schedules, metadata and badges.

Useful as a fallback and as the only free source with any boxing coverage at
all. The free tier is thin and inconsistent between sports, so this provider
sits low in the registry's priority order.

Docs: https://www.thesportsdb.com/api.php
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.providers.base import (
    BaseProvider,
    ProviderCapability,
    ProviderHealth,
    ProviderState,
    RawFixture,
    RawParticipant,
    RawResult,
)
from app.providers.http import ProviderClient

log = get_logger(__name__)

# Canonical league slug -> TheSportsDB numeric league id.
LEAGUE_IDS = {
    "premier-league": "4328",
    "championship": "4329",
    "la-liga": "4335",
    "serie-a": "4332",
    "bundesliga": "4331",
    "ligue-1": "4334",
    "eredivisie": "4337",
    "primeira-liga": "4344",
    "champions-league": "4480",
    "mls": "4346",
    "scottish-premiership": "4330",
    "nfl": "4391",
    "cfl": "4392",
    "nba": "4387",
    "wnba": "4516",
    "euroleague": "4497",
    "ufc": "4443",
    "pro-boxing": "4445",
}

STATUS_MAP = {
    "Match Finished": "finished",
    "FT": "finished",
    "AET": "finished",
    "Not Started": "scheduled",
    "NS": "scheduled",
    "Postponed": "postponed",
    "PP": "postponed",
    "Cancelled": "cancelled",
}


class TheSportsDbProvider(BaseProvider):
    name = "thesportsdb"
    supported_sports = {"football", "american-football", "basketball", "mma", "boxing"}
    supported_leagues = set(LEAGUE_IDS)
    capabilities = {ProviderCapability.FIXTURES, ProviderCapability.RESULTS}

    def __init__(self, api_key: str | None = None):
        # "3" is the documented free public key; a paid key raises the limits.
        self.api_key = (api_key if api_key is not None else get_settings().thesportsdb_key) or "3"
        self.client = ProviderClient(
            provider=self.name,
            base_url=f"https://www.thesportsdb.com/api/v1/json/{self.api_key}",
            rate=30,
            period=60.0,
        )

    def is_configured(self) -> bool:
        return True  # the free key always works, if thinly

    async def fetch_fixtures(self, league: str, start: date, end: date) -> list[RawFixture]:
        events = await self._events(league, "eventsnextleague.php")
        return [
            fixture
            for event in events
            if (fixture := self._to_fixture(event, league)) is not None
            and start <= fixture.start_time.date() <= end
        ]

    async def fetch_results(self, league: str, start: date, end: date) -> list[RawResult]:
        events = await self._events(league, "eventspastleague.php")
        results = []
        for event in events:
            event_date = _parse_date(event.get("dateEvent"))
            if event_date is None or not (start <= event_date <= end):
                continue
            results.append(
                RawResult(
                    external_id=str(event["idEvent"]),
                    league_code=league,
                    status=STATUS_MAP.get(event.get("strStatus", ""), "finished"),
                    home_score=_as_int(event.get("intHomeScore")),
                    away_score=_as_int(event.get("intAwayScore")),
                    detail={"round": event.get("intRound")},
                )
            )
        return results

    async def health(self) -> ProviderHealth:
        try:
            await self.client.get_json("/eventsnextleague.php", params={"id": "4328"}, ttl=600)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(
                name=self.name,
                state=ProviderState.DOWN,
                detail=str(exc),
                checked_at=datetime.now(UTC),
            )
        return ProviderHealth(name=self.name, state=ProviderState.UP, checked_at=datetime.now(UTC))

    async def _events(self, league: str, endpoint: str) -> list[dict[str, Any]]:
        league_id = LEAGUE_IDS.get(league)
        if league_id is None:
            raise ValueError(f"{self.name} does not cover league '{league}'")
        payload = await self.client.get_json(f"/{endpoint}", params={"id": league_id}, ttl=3600)
        # The free tier returns {"events": null} rather than an empty list.
        return payload.get("events") or []

    def _to_fixture(self, event: dict[str, Any], league: str) -> RawFixture | None:
        event_date = _parse_date(event.get("dateEvent"))
        if event_date is None:
            return None
        start_time = datetime.combine(event_date, _parse_time(event.get("strTime")), tzinfo=UTC)
        return RawFixture(
            external_id=str(event["idEvent"]),
            league_code=league,
            start_time=start_time,
            status=STATUS_MAP.get(event.get("strStatus", ""), "scheduled"),
            home=RawParticipant(
                name=event.get("strHomeTeam") or "",
                external_id=event.get("idHomeTeam"),
                is_home=True,
            ),
            away=RawParticipant(
                name=event.get("strAwayTeam") or "",
                external_id=event.get("idAwayTeam"),
                is_home=False,
            ),
            season=event.get("strSeason"),
            round=event.get("intRound"),
            event_name=event.get("strEvent"),
            venue=event.get("strVenue"),
        )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_time(value: str | None) -> time:
    if not value:
        return time(0, 0)
    try:
        return time.fromisoformat(value.replace("+00:00", "").strip())
    except ValueError:
        return time(0, 0)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
