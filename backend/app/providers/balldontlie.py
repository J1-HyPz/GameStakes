"""balldontlie adapter — free NBA data.

Free tier: 5 requests/minute, Authorization header holding the raw key (no
"Bearer" prefix). Docs: https://docs.balldontlie.io
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
    RawPlayerStat,
    RawResult,
)
from app.providers.http import ProviderClient

log = get_logger(__name__)

STATUS_MAP = {"Final": "finished"}


class BallDontLieProvider(BaseProvider):
    name = "balldontlie"
    supported_sports = {"basketball"}
    supported_leagues = {"nba"}
    capabilities = {
        ProviderCapability.FIXTURES,
        ProviderCapability.RESULTS,
        ProviderCapability.PLAYER_STATS,
    }

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key if api_key is not None else get_settings().balldontlie_key
        self.client = ProviderClient(
            provider=self.name,
            base_url="https://api.balldontlie.io/nba/v1",
            rate=5,  # free tier: 5 requests/minute
            period=60.0,
            headers={"Authorization": self.api_key} if self.api_key else {},
        )

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def fetch_fixtures(self, league: str, start: date, end: date) -> list[RawFixture]:
        games = await self._games(start, end)
        return [
            RawFixture(
                external_id=str(game["id"]),
                league_code=league,
                start_time=_start_time(game),
                status="finished" if game.get("status") == "Final" else "scheduled",
                home=RawParticipant(
                    name=_team_name(game.get("home_team", {})),
                    external_id=str(game.get("home_team", {}).get("id")),
                    is_home=True,
                ),
                away=RawParticipant(
                    name=_team_name(game.get("visitor_team", {})),
                    external_id=str(game.get("visitor_team", {}).get("id")),
                    is_home=False,
                ),
                season=str(game.get("season")) if game.get("season") else None,
            )
            for game in games
        ]

    async def fetch_results(self, league: str, start: date, end: date) -> list[RawResult]:
        games = await self._games(start, end)
        return [
            RawResult(
                external_id=str(game["id"]),
                league_code=league,
                status=STATUS_MAP.get(game.get("status", ""), "scheduled"),
                home_score=game.get("home_team_score"),
                away_score=game.get("visitor_team_score"),
                detail={"period": game.get("period"), "postseason": game.get("postseason")},
            )
            for game in games
            if game.get("status") == "Final"
        ]

    async def fetch_player_stats(self, league: str, season: str) -> list[RawPlayerStat]:
        payload = await self.client.get_json(
            "/stats",
            params={"seasons[]": season, "per_page": 100},
            ttl=24 * 3600,
        )
        stats: list[RawPlayerStat] = []
        for row in payload.get("data", []):
            player = row.get("player", {})
            team = row.get("team", {})
            stats.append(
                RawPlayerStat(
                    player_name=" ".join(
                        filter(None, [player.get("first_name"), player.get("last_name")])
                    ),
                    external_id=str(player.get("id")) if player.get("id") else None,
                    league_code=league,
                    season=season,
                    team_name=_team_name(team),
                    fixture_external_id=str(row.get("game", {}).get("id")),
                    minutes=_minutes(row.get("min")),
                    stats={
                        k: row.get(k)
                        for k in ("pts", "reb", "ast", "stl", "blk", "fg3m", "turnover")
                    },
                )
            )
        return stats

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(
                name=self.name,
                state=ProviderState.DISABLED,
                detail="BALLDONTLIE_KEY not set",
            )
        try:
            await self.client.get_json("/teams", params={"per_page": 1}, ttl=3600)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(
                name=self.name,
                state=ProviderState.DOWN,
                detail=str(exc),
                checked_at=datetime.now(UTC),
            )
        return ProviderHealth(name=self.name, state=ProviderState.UP, checked_at=datetime.now(UTC))

    async def _games(self, start: date, end: date) -> list[dict[str, Any]]:
        payload = await self.client.get_json(
            "/games",
            params={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "per_page": 100,
            },
            ttl=3600,
        )
        games: list[dict[str, Any]] = payload.get("data", [])
        return games


def _team_name(team: dict[str, Any]) -> str:
    return team.get("full_name") or team.get("name") or ""


def _start_time(game: dict[str, Any]) -> datetime:
    # `status` carries an ISO timestamp before tip-off and "Final" afterwards.
    raw = game.get("status", "")
    if raw and raw[0].isdigit():
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.fromisoformat(f"{game['date']}T00:00:00+00:00")


def _minutes(value: Any) -> float | None:
    """balldontlie reports minutes as "34" or "34:12"."""
    if not value:
        return None
    text = str(value)
    if ":" in text:
        minutes, _, seconds = text.partition(":")
        try:
            return int(minutes) + int(seconds) / 60
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None
