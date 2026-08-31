"""Entity-resolution admin endpoints: review the queue, apply decisions."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import SessionDep
from app.db.enums import EntityType, ResolutionStatus
from app.db.models import ResolutionQueueItem
from app.ingest.resolution import EntityResolver, InvalidResolutionError

router = APIRouter(prefix="/resolution", tags=["resolution"])


class QueueItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: EntityType
    sport_id: int | None
    league_id: int | None
    provider: str
    raw_name: str
    external_id: str | None
    candidates: list[Any]
    status: ResolutionStatus
    resolved_entity_id: int | None


class ResolveIn(BaseModel):
    entity_id: int


@router.get("/queue")
async def list_queue(
    session: SessionDep,
    status: ResolutionStatus = ResolutionStatus.PENDING,
) -> list[QueueItemOut]:
    stmt = (
        select(ResolutionQueueItem)
        .where(ResolutionQueueItem.status == status)
        .order_by(ResolutionQueueItem.created_at)
    )
    items = (await session.execute(stmt)).scalars().all()
    return [QueueItemOut.model_validate(item) for item in items]


@router.post("/queue/{item_id}/resolve")
async def resolve_item(
    item_id: int,
    body: ResolveIn,
    session: SessionDep,
) -> QueueItemOut:
    resolver = EntityResolver(session)
    try:
        item = await resolver.resolve_manually(item_id, body.entity_id)
    except InvalidResolutionError as exc:
        # The queue item exists; the chosen entity is unusable.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return QueueItemOut.model_validate(item)


@router.post("/queue/{item_id}/ignore")
async def ignore_item(
    item_id: int,
    session: SessionDep,
) -> QueueItemOut:
    resolver = EntityResolver(session)
    try:
        item = await resolver.ignore(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return QueueItemOut.model_validate(item)
