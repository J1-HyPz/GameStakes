"""football-data.org adapter — free European football fixtures and results.

Free tier: 10 requests/minute, 12 competitions, X-Auth-Token header.
Docs: https://docs.football-data.org/general/v4/
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
    RawTeamStat,
)
from app.providers.http import ProviderClient

log = get_logger(__name__)

# Canonical league slug -> football-data.org competition code. Only the free
# tier's competitions are listed; anything else 403s.
LEAGUE_CODES = {
    "premier-league": "PL",
    "championship": "ELC",
    "la-liga": "PD",
    "serie-a": "SA",
    "bundesliga": "BL1",
    "ligue-1": "FL1",
    "eredivisie": "DED",
    "primeira-liga": "PPL",
    "champions-league": "CL",
    "world-cup": "WC",
    "copa-america": "CLI",
    "euros": "EC",
}

# football-data.org status -> canonical FixtureStatus value.
STATUS_MAP = {
    "SCHEDULED": "scheduled",
    "TIMED": "scheduled",
    "IN_PLAY": "in_play",
    "PAUSED": "in_play",
    "FINISHED": "finished",
    "AWARDED": "finished",
    "SUSPENDED": "postponed",
    "POSTPONED": "postponed",
    "CANCELLED": "cancelled",
}


class FootballDataProvider(BaseProvider):
    name = "football-data.org"
    supported_sports = {"football"}
    supported_leagues = set(LEAGUE_CODES)
    capabilities = {
        ProviderCapability.FIXTURES,
        ProviderCapability.RESULTS,
        ProviderCapability.TEAM_STATS,
    }

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key if api_key is not None else get_settings().football_data_org_key
        self.client = ProviderClient(
            provider=self.name,
            base_url="https://api.football-data.org/v4",
            rate=10,  # free tier: 10 requests/minute
            period=60.0,
            headers={"X-Auth-Token": self.api_key} if self.api_key else {},
        )

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def fetch_fixtures(self, league: str, start: date, end: date) -> list[RawFixture]:
        matches = await self._matches(league, start, end)
        return [self._to_fixture(m, league) for m in matches]

    async def fetch_results(self, league: str, start: date, end: date) -> list[RawResult]:
        matches = await self._matches(league, start, end, status="FINISHED")
        return [self._to_result(m, league) for m in matches]

    async def fetch_team_stats(self, league: str, season: str) -> list[RawTeamStat]:
        code = self._code(league)
        payload = await self.client.get_json(
            f"/competitions/{code}/standings",
            params={"season": season} if season else None,
            ttl=6 * 3600,
        )
        stats: list[RawTeamStat] = []
        for table in payload.get("standings", []):
            if table.get("type") != "TOTAL":
                continue
            for row in table.get("table", []):
                team = row.get("team", {})
                stats.append(
                    RawTeamStat(
                        team_name=team.get("name", ""),
                        external_id=str(team.get("id")) if team.get("id") else None,
                        league_code=league,
                        season=season,
                        played=row.get("playedGames", 0),
                        wins=row.get("won", 0),
                        draws=row.get("draw", 0),
                        losses=row.get("lost", 0),
                        score_for=row.get("goalsFor"),
                        score_against=row.get("goalsAgainst"),
                        extra={"points": row.get("points"), "position": row.get("position")},
                    )
                )
        return stats

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(
                name=self.name,
                state=ProviderState.DISABLED,
                detail="FOOTBALL_DATA_ORG_KEY not set",
            )
        try:
            await self.client.get_json("/competitions/PL", ttl=300)
        except Exception as exc:  # noqa: BLE001 — health must never raise
            return ProviderHealth(
                name=self.name,
                state=ProviderState.DOWN,
                detail=str(exc),
                checked_at=datetime.now(UTC),
            )
        return ProviderHealth(name=self.name, state=ProviderState.UP, checked_at=datetime.now(UTC))

    # -- internals -----------------------------------------------------------

    def _code(self, league: str) -> str:
        code = LEAGUE_CODES.get(league)
        if code is None:
            raise ValueError(f"{self.name} does not cover league '{league}'")
        return code

    async def _matches(
        self, league: str, start: date, end: date, status: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
        }
        if status:
            params["status"] = status
        payload = await self.client.get_json(
            f"/competitions/{self._code(league)}/matches",
            params=params,
            # Finished results are stable; upcoming fixtures move around.
            ttl=6 * 3600 if status == "FINISHED" else 3600,
        )
        matches: list[dict[str, Any]] = payload.get("matches", [])
        return matches

    def _to_fixture(self, match: dict[str, Any], league: str) -> RawFixture:
        home, away = match.get("homeTeam", {}), match.get("awayTeam", {})
        season = match.get("season") or {}
        return RawFixture(
            external_id=str(match["id"]),
            league_code=league,
            start_time=datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00")),
            status=STATUS_MAP.get(match.get("status", ""), "unknown"),
            home=RawParticipant(
                name=home.get("name") or home.get("shortName") or "",
                external_id=str(home["id"]) if home.get("id") else None,
                is_home=True,
            ),
            away=RawParticipant(
                name=away.get("name") or away.get("shortName") or "",
                external_id=str(away["id"]) if away.get("id") else None,
                is_home=False,
            ),
            season=str(season.get("startDate", ""))[:4] or None,
            round=str(match["matchday"]) if match.get("matchday") else match.get("stage"),
        )

    def _to_result(self, match: dict[str, Any], league: str) -> RawResult:
        score = match.get("score", {})
        full_time = score.get("fullTime", {})
        return RawResult(
            external_id=str(match["id"]),
            league_code=league,
            status=STATUS_MAP.get(match.get("status", ""), "unknown"),
            home_score=full_time.get("home"),
            away_score=full_time.get("away"),
            detail={
                "half_time": score.get("halfTime", {}),
                "winner": score.get("winner"),
                "duration": score.get("duration"),
            },
        )
