"""ESPN public JSON adapter — broad coverage, no contract.

These endpoints are undocumented and unversioned: they change without notice
and can disappear entirely. That is acceptable here because the interface
isolates it — a break degrades to "ESPN unavailable" in the UI and the registry
falls through to another provider. Treat every field as optional and never let
this adapter be the only source for a league you care about.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

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

# Canonical league slug -> ESPN (sport, league) path segments.
LEAGUE_PATHS = {
    "premier-league": ("soccer", "eng.1"),
    "championship": ("soccer", "eng.2"),
    "la-liga": ("soccer", "esp.1"),
    "serie-a": ("soccer", "ita.1"),
    "bundesliga": ("soccer", "ger.1"),
    "ligue-1": ("soccer", "fra.1"),
    "eredivisie": ("soccer", "ned.1"),
    "primeira-liga": ("soccer", "por.1"),
    "champions-league": ("soccer", "uefa.champions"),
    "europa-league": ("soccer", "uefa.europa"),
    "conference-league": ("soccer", "uefa.europa.conf"),
    "mls": ("soccer", "usa.1"),
    "liga-mx": ("soccer", "mex.1"),
    "scottish-premiership": ("soccer", "sco.1"),
    "nfl": ("football", "nfl"),
    "ncaa-fbs": ("football", "college-football"),
    "nba": ("basketball", "nba"),
    "wnba": ("basketball", "wnba"),
    "ncaa-mbb": ("basketball", "mens-college-basketball"),
    "ncaa-wbb": ("basketball", "womens-college-basketball"),
}

STATUS_MAP = {
    "STATUS_SCHEDULED": "scheduled",
    "STATUS_IN_PROGRESS": "in_play",
    "STATUS_HALFTIME": "in_play",
    "STATUS_END_PERIOD": "in_play",
    "STATUS_FINAL": "finished",
    "STATUS_FULL_TIME": "finished",
    "STATUS_POSTPONED": "postponed",
    "STATUS_CANCELED": "cancelled",
}


class EspnProvider(BaseProvider):
    name = "espn"
    supported_sports = {"football", "american-football", "basketball"}
    supported_leagues = set(LEAGUE_PATHS)
    capabilities = {ProviderCapability.FIXTURES, ProviderCapability.RESULTS}
    # Flagged best-effort so the UI can say so rather than implying a contract.
    best_effort = True

    def __init__(self) -> None:
        self.client = ProviderClient(
            provider=self.name,
            base_url="https://site.api.espn.com/apis/site/v2/sports",
            rate=60,
            period=60.0,
        )

    def is_configured(self) -> bool:
        return True  # no key required

    async def fetch_fixtures(self, league: str, start: date, end: date) -> list[RawFixture]:
        events = await self._scoreboard(league, start, end)
        return [f for e in events if (f := self._to_fixture(e, league)) is not None]

    async def fetch_results(self, league: str, start: date, end: date) -> list[RawResult]:
        events = await self._scoreboard(league, start, end)
        results = []
        for event in events:
            competition = _first_competition(event)
            if competition is None:
                continue
            status = _status(event)
            if status != "finished":
                continue
            home, away = _home_away(competition)
            results.append(
                RawResult(
                    external_id=str(event["id"]),
                    league_code=league,
                    status=status,
                    home_score=_as_int((home or {}).get("score")),
                    away_score=_as_int((away or {}).get("score")),
                    detail={"attendance": competition.get("attendance")},
                )
            )
        return results

    async def health(self) -> ProviderHealth:
        try:
            await self.client.get_json("/soccer/eng.1/scoreboard", ttl=300)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(
                name=self.name,
                state=ProviderState.DOWN,
                detail=f"undocumented endpoints; may have changed: {exc}",
                checked_at=datetime.now(UTC),
            )
        return ProviderHealth(
            name=self.name,
            state=ProviderState.UP,
            detail="best-effort: undocumented endpoints, may break without notice",
            checked_at=datetime.now(UTC),
        )

    async def _scoreboard(self, league: str, start: date, end: date) -> list[dict[str, Any]]:
        path = LEAGUE_PATHS.get(league)
        if path is None:
            raise ValueError(f"{self.name} does not cover league '{league}'")
        sport, league_path = path
        payload = await self.client.get_json(
            f"/{sport}/{league_path}/scoreboard",
            params={"dates": f"{start:%Y%m%d}-{end:%Y%m%d}", "limit": 300},
            ttl=1800,
        )
        events: list[dict[str, Any]] = payload.get("events", [])
        return events

    def _to_fixture(self, event: dict[str, Any], league: str) -> RawFixture | None:
        competition = _first_competition(event)
        if competition is None:
            return None
        home, away = _home_away(competition)
        if home is None or away is None:
            return None
        try:
            start_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return None
        return RawFixture(
            external_id=str(event["id"]),
            league_code=league,
            start_time=start_time,
            status=_status(event),
            home=RawParticipant(name=_team_name(home), external_id=_team_id(home), is_home=True),
            away=RawParticipant(name=_team_name(away), external_id=_team_id(away), is_home=False),
            venue=(competition.get("venue") or {}).get("fullName"),
        )


def _first_competition(event: dict[str, Any]) -> dict[str, Any] | None:
    competitions = event.get("competitions") or []
    return competitions[0] if competitions else None


def _home_away(
    competition: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    home = away = None
    for competitor in competition.get("competitors", []):
        if competitor.get("homeAway") == "home":
            home = competitor
        elif competitor.get("homeAway") == "away":
            away = competitor
    return home, away


def _team_name(competitor: dict[str, Any]) -> str:
    team = competitor.get("team", {})
    return team.get("displayName") or team.get("name") or ""


def _team_id(competitor: dict[str, Any]) -> str | None:
    team_id = competitor.get("team", {}).get("id")
    return str(team_id) if team_id else None


def _status(event: dict[str, Any]) -> str:
    name = (
        event.get("status", {}).get("type", {}).get("name")
        or (_first_competition(event) or {}).get("status", {}).get("type", {}).get("name")
        or ""
    )
    return STATUS_MAP.get(name, "unknown")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
