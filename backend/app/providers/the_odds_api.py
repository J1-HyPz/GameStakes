"""The Odds API adapter — bookmaker prices across sports and markets.

Credits are the binding constraint: the free tier is 500 a month and an odds
call costs one credit *per market per region*, so a single greedy poll across
several markets can burn a day's budget. Every call here is deliberate about
what it asks for, `/sports` and `/events` are free and used where they can
substitute, and remaining quota is read from the response headers and surfaced
in the UI.

Docs: https://the-odds-api.com/liveapi/guides/v4/
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.providers.base import (
    BaseProvider,
    Market,
    ProviderCapability,
    ProviderHealth,
    ProviderState,
    RawFixture,
    RawOdds,
    RawParticipant,
)
from app.providers.http import ProviderClient

log = get_logger(__name__)

# Canonical league slug -> The Odds API sport key.
SPORT_KEYS = {
    "premier-league": "soccer_epl",
    "championship": "soccer_efl_champ",
    "la-liga": "soccer_spain_la_liga",
    "serie-a": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga",
    "ligue-1": "soccer_france_ligue_one",
    "eredivisie": "soccer_netherlands_eredivisie",
    "primeira-liga": "soccer_portugal_primeira_liga",
    "champions-league": "soccer_uefa_champs_league",
    "europa-league": "soccer_uefa_europa_league",
    "conference-league": "soccer_uefa_europa_conference_league",
    "mls": "soccer_usa_mls",
    "scottish-premiership": "soccer_spl",
    "nfl": "americanfootball_nfl",
    "ncaa-fbs": "americanfootball_ncaaf",
    "cfl": "americanfootball_cfl",
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "ncaa-mbb": "basketball_ncaab",
    "euroleague": "basketball_euroleague",
    "ufc": "mma_mixed_martial_arts",
    "pro-boxing": "boxing_boxing",
}

# Canonical market -> The Odds API market key.
MARKET_KEYS = {
    Market.MATCH_WINNER: "h2h",
    Market.TOTALS: "totals",
    Market.SPREADS: "spreads",
}

# Their outcome names are team names; these map the non-team ones.
_DRAW_NAMES = {"draw"}


class TheOddsApiProvider(BaseProvider):
    name = "the-odds-api"
    supported_sports = {"football", "american-football", "basketball", "mma", "boxing"}
    supported_leagues = set(SPORT_KEYS)
    capabilities = {ProviderCapability.ODDS, ProviderCapability.FIXTURES}

    def __init__(self, api_key: str | None = None, region: str | None = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.the_odds_api_key
        self.region = region if region is not None else settings.odds_region
        self.client = ProviderClient(
            provider=self.name,
            base_url="https://api.the-odds-api.com/v4",
            rate=30,
            period=60.0,
        )

    def is_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def quota_remaining(self) -> int | None:
        return self.client.quota_remaining

    async def fetch_fixtures(self, league: str, start: date, end: date) -> list[RawFixture]:
        """Event list only — this endpoint is free, so it never costs credits."""
        payload = await self.client.get_json(
            f"/sports/{self._sport_key(league)}/events",
            params={
                "apiKey": self.api_key,
                "dateFormat": "iso",
                "commenceTimeFrom": _iso_z(start),
                "commenceTimeTo": _iso_z(end, end_of_day=True),
            },
            ttl=3600,
        )
        return [
            RawFixture(
                external_id=event["id"],
                league_code=league,
                start_time=datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00")),
                status="scheduled",
                home=RawParticipant(name=event.get("home_team", ""), is_home=True),
                away=RawParticipant(name=event.get("away_team", ""), is_home=False),
            )
            for event in payload
            if event.get("home_team") and event.get("away_team")
        ]

    async def fetch_odds(self, league: str, markets: list[Market]) -> list[RawOdds]:
        """One call covering the whole league slate.

        Cost is markets x regions, so asking for three markets in one region
        bills three credits regardless of how many fixtures come back — always
        cheaper than per-fixture calls.
        """
        keys = [MARKET_KEYS[m] for m in markets if m in MARKET_KEYS]
        if not keys:
            return []

        payload = await self.client.get_json(
            f"/sports/{self._sport_key(league)}/odds",
            params={
                "apiKey": self.api_key,
                "regions": self.region,
                "markets": ",".join(keys),
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
            ttl=300,  # 5 minutes; odds move, but credits are scarcer
            cost=float(len(keys)),
        )

        captured_at = datetime.now(UTC)
        odds: list[RawOdds] = []
        for event in payload:
            home, away = event.get("home_team"), event.get("away_team")
            for book in event.get("bookmakers", []):
                for market in book.get("markets", []):
                    market_key = _canonical_market(market.get("key", ""))
                    for outcome in market.get("outcomes", []):
                        selection = _canonical_selection(outcome.get("name", ""), home, away)
                        odds.append(
                            RawOdds(
                                fixture_external_id=event["id"],
                                bookmaker=book.get("title") or book.get("key", ""),
                                market=market_key,
                                selection=selection,
                                price_decimal=float(outcome["price"]),
                                line=(
                                    float(outcome["point"])
                                    if outcome.get("point") is not None
                                    else None
                                ),
                                captured_at=captured_at,
                            )
                        )
        return odds

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(
                name=self.name,
                state=ProviderState.DISABLED,
                detail="THE_ODDS_API_KEY not set — the bet builder needs prices",
            )
        try:
            # /sports is free: a health check must not spend credits.
            await self.client.get_json("/sports", params={"apiKey": self.api_key}, ttl=600)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(
                name=self.name,
                state=ProviderState.DOWN,
                detail=str(exc),
                checked_at=datetime.now(UTC),
            )

        remaining = self.client.quota_remaining
        low_quota = remaining is not None and remaining < 50
        return ProviderHealth(
            name=self.name,
            state=ProviderState.DEGRADED if low_quota else ProviderState.UP,
            detail=(f"only {remaining} credits left this period" if low_quota else None),
            quota_remaining=remaining,
            checked_at=datetime.now(UTC),
        )

    def _sport_key(self, league: str) -> str:
        key = SPORT_KEYS.get(league)
        if key is None:
            raise ValueError(f"{self.name} does not cover league '{league}'")
        return key


def _canonical_market(key: str) -> str:
    return {"h2h": "1x2", "totals": "totals", "spreads": "spreads"}.get(key, key)


def _canonical_selection(name: str, home: str | None, away: str | None) -> str:
    """Their outcomes are named by team; ours are home/draw/away positions."""
    if name.casefold() in _DRAW_NAMES:
        return "draw"
    if home and name == home:
        return "home"
    if away and name == away:
        return "away"
    return name.casefold()  # over/under, or a player name on props


def _iso_z(day: date, end_of_day: bool = False) -> str:
    """The API wants ISO8601 without sub-second precision, Z-suffixed."""
    moment = datetime.combine(day, datetime.max.time() if end_of_day else datetime.min.time())
    return moment.replace(microsecond=0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
