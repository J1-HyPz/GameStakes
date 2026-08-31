"""Entity-resolution admin endpoints."""

from fastapi.testclient import TestClient


def _queue_one(client: TestClient) -> dict[str, object]:
    """Create a pending queue item by resolving an unmatchable name."""
    import asyncio

    from app.db.session import get_sessionmaker
    from app.ingest.resolution import EntityResolver

    async def run() -> int:
        async with get_sessionmaker()() as session:
            from sqlalchemy import select

            from app.db.models import Sport

            sport = (
                await session.execute(select(Sport).where(Sport.slug == "football"))
            ).scalar_one()
            resolver = EntityResolver(session)
            result = await resolver.resolve_team("api-test", "Unmatchable XI", sport.id)
            await session.commit()
            assert result.queue_item_id is not None
            return result.queue_item_id

    item_id = asyncio.run(run())
    items = client.get("/api/resolution/queue").json()
    return next(item for item in items if item["id"] == item_id)


def test_queue_lists_pending_items(client: TestClient) -> None:
    item = _queue_one(client)
    assert item["status"] == "pending"
    assert item["raw_name"] == "Unmatchable XI"
    assert item["entity_type"] == "team"


def test_resolve_with_nonexistent_entity_is_422_not_404(client: TestClient) -> None:
    item = _queue_one(client)
    response = client.post(
        f"/api/resolution/queue/{item['id']}/resolve", json={"entity_id": 999_999}
    )
    assert response.status_code == 422
    assert "does not exist" in response.json()["detail"]


def test_resolve_on_missing_queue_item_is_404(client: TestClient) -> None:
    response = client.post("/api/resolution/queue/999999/resolve", json={"entity_id": 1})
    assert response.status_code == 404


def test_ignore_closes_the_item(client: TestClient) -> None:
    item = _queue_one(client)
    response = client.post(f"/api/resolution/queue/{item['id']}/ignore")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
