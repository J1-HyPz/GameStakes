"""Adapter parsing and registry failover.

Adapters are driven with recorded-shape payloads through a mock transport, so
these tests assert our normalisation, not the upstream's uptime.
"""

from datetime import UTC, date, datetime

import httpx
import pytest

from app.core.exceptions import ProviderError
from app.providers.base import (
    BaseProvider,
    Market,
    ProviderCapability,
    ProviderHealth,
    ProviderState,
    RawFixture,
    RawParticipant,
)
from app.providers.cache import InProcessCache
from app.providers.football_data import FootballDataProvider
from app.providers.registry import ProviderRegistry, describe_providers
from app.providers.the_odds_api import TheOddsApiProvider
from app.providers.thesportsdb import TheSportsDbProvider


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = InProcessCache()
    monkeypatch.setattr("app.providers.http.get_cache", lambda: cache)


def _mount(provider: BaseProvider, handler: httpx.MockTransport) -> None:
    client = provider.client  # type: ignore[attr-defined]
    client._client = httpx.AsyncClient(
        transport=handler, base_url=client.base_url, headers=client.headers
    )


FOOTBALL_DATA_MATCHES = {
    "matches": [
        {
            "id": 497763,
            "utcDate": "2026-09-12T14:00:00Z",
            "status": "FINISHED",
            "matchday": 4,
            "season": {"startDate": "2026-08-15"},
            "homeTeam": {"id": 66, "name": "Manchester United FC"},
            "awayTeam": {"id": 61, "name": "Chelsea FC"},
            "score": {
                "winner": "HOME_TEAM",
                "duration": "REGULAR",
                "fullTime": {"home": 2, "away": 1},
                "halfTime": {"home": 1, "away": 0},
            },
        }
    ]
}


class TestFootballDataProvider:
    async def test_fixtures_are_normalised(self) -> None:
        provider = FootballDataProvider(api_key="test-key")
        _mount(
            provider, httpx.MockTransport(lambda r: httpx.Response(200, json=FOOTBALL_DATA_MATCHES))
        )

        fixtures = await provider.fetch_fixtures(
            "premier-league", date(2026, 9, 1), date(2026, 9, 30)
        )

        assert len(fixtures) == 1
        fixture = fixtures[0]
        assert fixture.external_id == "497763"
        assert fixture.start_time == datetime(2026, 9, 12, 14, 0, tzinfo=UTC)
        assert fixture.status == "finished"
        assert fixture.home.name == "Manchester United FC"
        assert fixture.home.external_id == "66"
        assert fixture.away.is_home is False

    async def test_results_carry_scores_and_half_time(self) -> None:
        provider = FootballDataProvider(api_key="test-key")
        _mount(
            provider, httpx.MockTransport(lambda r: httpx.Response(200, json=FOOTBALL_DATA_MATCHES))
        )

        results = await provider.fetch_results(
            "premier-league", date(2026, 9, 1), date(2026, 9, 30)
        )

        assert results[0].home_score == 2
        assert results[0].away_score == 1
        assert results[0].detail["half_time"] == {"home": 1, "away": 0}

    async def test_unknown_league_is_rejected_before_any_request(self) -> None:
        provider = FootballDataProvider(api_key="test-key")
        with pytest.raises(ValueError, match="does not cover"):
            await provider.fetch_fixtures("nba", date(2026, 9, 1), date(2026, 9, 2))

    async def test_missing_key_reports_disabled_without_calling_out(self) -> None:
        provider = FootballDataProvider(api_key="")
        health = await provider.health()
        assert health.state == ProviderState.DISABLED
        assert "FOOTBALL_DATA_ORG_KEY" in (health.detail or "")


ODDS_PAYLOAD = [
    {
        "id": "e1",
        "sport_key": "soccer_epl",
        "commence_time": "2026-09-12T14:00:00Z",
        "home_team": "Manchester United",
        "away_team": "Chelsea",
        "bookmakers": [
            {
                "key": "bet365",
                "title": "Bet365",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Manchester United", "price": 2.1},
                            {"name": "Chelsea", "price": 3.4},
                            {"name": "Draw", "price": 3.6},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": 1.9, "point": 2.5},
                            {"name": "Under", "price": 1.95, "point": 2.5},
                        ],
                    },
                ],
            }
        ],
    }
]


class TestTheOddsApiProvider:
    async def test_outcomes_map_to_home_draw_away(self) -> None:
        provider = TheOddsApiProvider(api_key="test-key", region="uk")
        _mount(provider, httpx.MockTransport(lambda r: httpx.Response(200, json=ODDS_PAYLOAD)))

        odds = await provider.fetch_odds("premier-league", [Market.MATCH_WINNER, Market.TOTALS])

        selections = {(o.market, o.selection): o.price_decimal for o in odds}
        assert selections[("1x2", "home")] == 2.1
        assert selections[("1x2", "away")] == 3.4
        assert selections[("1x2", "draw")] == 3.6
        assert selections[("totals", "over")] == 1.9
        assert next(o for o in odds if o.selection == "over").line == 2.5
        assert all(o.bookmaker == "Bet365" for o in odds)

    async def test_odds_call_bills_one_credit_per_market(self) -> None:
        provider = TheOddsApiProvider(api_key="test-key", region="uk")
        _mount(provider, httpx.MockTransport(lambda r: httpx.Response(200, json=ODDS_PAYLOAD)))
        before = provider.client.bucket._tokens

        await provider.fetch_odds("premier-league", [Market.MATCH_WINNER, Market.TOTALS])

        spent = before - provider.client.bucket._tokens
        assert spent >= 2.0, "two markets must cost two credits, not one call"

    async def test_no_supported_markets_makes_no_request(self) -> None:
        provider = TheOddsApiProvider(api_key="test-key")

        def explode(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not spend a credit for unsupported markets")

        _mount(provider, httpx.MockTransport(explode))
        assert await provider.fetch_odds("premier-league", [Market.CORRECT_SCORE]) == []

    async def test_health_uses_the_free_sports_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json=[], headers={"x-requests-remaining": "412"})

        provider = TheOddsApiProvider(api_key="test-key")
        _mount(provider, httpx.MockTransport(handler))
        health = await provider.health()

        assert seen == ["/v4/sports"], "health must not hit a metered endpoint"
        assert health.state == ProviderState.UP
        assert health.quota_remaining == 412

    async def test_low_quota_is_reported_as_degraded(self) -> None:
        provider = TheOddsApiProvider(api_key="test-key")
        _mount(
            provider,
            httpx.MockTransport(
                lambda r: httpx.Response(200, json=[], headers={"x-requests-remaining": "12"})
            ),
        )
        health = await provider.health()
        assert health.state == ProviderState.DEGRADED
        assert "12 credits" in (health.detail or "")


class TestTheSportsDbProvider:
    async def test_null_events_are_treated_as_empty(self) -> None:
        """The free tier returns {"events": null} rather than an empty list."""
        provider = TheSportsDbProvider(api_key="3")
        _mount(provider, httpx.MockTransport(lambda r: httpx.Response(200, json={"events": None})))
        assert await provider.fetch_fixtures("nba", date(2026, 9, 1), date(2026, 9, 30)) == []


class _StubProvider(BaseProvider):
    """Registry test double."""

    def __init__(
        self, name: str, *, leagues: set[str], fail: bool = False, configured: bool = True
    ):
        self.name = name
        self.supported_leagues = leagues
        self.capabilities = {ProviderCapability.FIXTURES}
        self._fail = fail
        self._configured = configured
        self.calls = 0

    def is_configured(self) -> bool:
        return self._configured

    async def fetch_fixtures(self, league: str, start: date, end: date) -> list[RawFixture]:
        self.calls += 1
        if self._fail:
            raise ProviderError(f"{self.name} is down")
        return [
            RawFixture(
                external_id=f"{self.name}-1",
                league_code=league,
                start_time=datetime(2026, 9, 12, 14, 0, tzinfo=UTC),
                status="scheduled",
                home=RawParticipant(name="Home", is_home=True),
                away=RawParticipant(name="Away", is_home=False),
            )
        ]

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            state=ProviderState.DOWN if self._fail else ProviderState.UP,
        )


class TestRegistry:
    async def test_first_healthy_provider_wins(self) -> None:
        primary = _StubProvider("primary", leagues={"premier-league"})
        backup = _StubProvider("backup", leagues={"premier-league"})
        registry = ProviderRegistry([primary, backup])

        outcome = await registry.fetch_fixtures(
            "premier-league", date(2026, 9, 1), date(2026, 9, 30)
        )

        assert outcome.provider == "primary"
        assert backup.calls == 0
        assert not outcome.degraded

    async def test_failure_falls_through_and_is_reported(self) -> None:
        broken = _StubProvider("broken", leagues={"premier-league"}, fail=True)
        backup = _StubProvider("backup", leagues={"premier-league"})
        registry = ProviderRegistry([broken, backup])

        outcome = await registry.fetch_fixtures(
            "premier-league", date(2026, 9, 1), date(2026, 9, 30)
        )

        assert outcome.provider == "backup"
        assert outcome.data[0].external_id == "backup-1"
        assert outcome.degraded, "the UI must be able to say a source failed"
        assert [(a.provider, a.ok) for a in outcome.attempts] == [
            ("broken", False),
            ("backup", True),
        ]

    async def test_unconfigured_providers_are_skipped(self) -> None:
        unconfigured = _StubProvider("nokey", leagues={"premier-league"}, configured=False)
        backup = _StubProvider("backup", leagues={"premier-league"})
        registry = ProviderRegistry([unconfigured, backup])

        outcome = await registry.fetch_fixtures(
            "premier-league", date(2026, 9, 1), date(2026, 9, 30)
        )

        assert outcome.provider == "backup"
        assert unconfigured.calls == 0
        assert not outcome.degraded, "a missing key is not a failure"

    async def test_no_coverage_returns_empty_without_raising(self) -> None:
        registry = ProviderRegistry([_StubProvider("only-epl", leagues={"premier-league"})])

        outcome = await registry.fetch_fixtures("ufc", date(2026, 9, 1), date(2026, 9, 30))

        assert outcome.data == []
        assert outcome.provider is None

    async def test_all_failing_returns_empty_and_records_every_attempt(self) -> None:
        registry = ProviderRegistry(
            [
                _StubProvider("a", leagues={"nba"}, fail=True),
                _StubProvider("b", leagues={"nba"}, fail=True),
            ]
        )

        outcome = await registry.fetch_fixtures("nba", date(2026, 9, 1), date(2026, 9, 30))

        assert outcome.data == []
        assert outcome.provider is None
        assert len(outcome.attempts) == 2
        assert outcome.degraded

    def test_describe_providers_needs_no_network(self) -> None:
        registry = ProviderRegistry([_StubProvider("x", leagues={"nba"})])
        described = describe_providers(registry)
        assert described[0]["name"] == "x"
        assert described[0]["leagues"] == ["nba"]
        assert described[0]["priority"] == 0
