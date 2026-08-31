"""Catalogue API endpoints."""

from fastapi.testclient import TestClient


def test_sports_lists_all_five_with_league_counts(client: TestClient) -> None:
    response = client.get("/api/sports")
    assert response.status_code == 200
    sports = {s["slug"]: s for s in response.json()}
    assert set(sports) == {"football", "american-football", "basketball", "boxing", "mma"}
    assert sports["football"]["league_count"] == 20
    assert sports["boxing"]["kind"] == "combat"


def test_leagues_filter_by_sport(client: TestClient) -> None:
    response = client.get("/api/leagues", params={"sport": "mma"})
    assert response.status_code == 200
    slugs = {le["slug"] for le in response.json()}
    assert slugs == {"ufc", "pfl", "one-championship"}
    assert all(le["sport_slug"] == "mma" for le in response.json())


def test_league_detail_and_404(client: TestClient) -> None:
    ok = client.get("/api/leagues/premier-league")
    assert ok.status_code == 200
    assert ok.json()["name"] == "Premier League"

    missing = client.get("/api/leagues/superleague-breakaway")
    assert missing.status_code == 404
