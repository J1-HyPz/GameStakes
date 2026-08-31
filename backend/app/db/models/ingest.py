"""Ingestion bookkeeping and entity resolution.

- raw_ingest keeps provider payloads verbatim so models can be rebuilt without
  refetching.
- ingest_jobs makes every job's outcome and row counts visible — no silent
  failures.
- entity_aliases maps provider names/ids to canonical entities; unresolvable
  names land in resolution_queue for the manual-override admin screen.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import (
    AliasSource,
    EntityType,
    JobStatus,
    ResolutionStatus,
    db_enum,
    enum_check,
)
from app.db.types import JSONVariant, TZDateTime


class IngestJob(Base):
    __tablename__ = "ingest_jobs"
    __table_args__ = (
        Index("ix_ingest_jobs_name_started", "job_name", "started_at"),
        enum_check("status", JobStatus),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[JobStatus] = mapped_column(db_enum(JobStatus, "job_status"), index=True)
    started_at: Mapped[datetime] = mapped_column(TZDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    rows_fetched: Mapped[int] = mapped_column(Integer, default=0)
    rows_upserted: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)


class RawIngest(Base):
    __tablename__ = "raw_ingest"
    __table_args__ = (
        Index("ix_raw_ingest_provider_endpoint_time", "provider", "endpoint", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    endpoint: Mapped[str] = mapped_column(String(200))
    params: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    payload: Mapped[Any] = mapped_column(JSONVariant)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)


class EntityAlias(Base):
    """Provider name/id -> canonical entity. entity_id is polymorphic across
    teams/players/leagues/fixtures (no FK by design); entity_type scopes it."""

    __tablename__ = "entity_aliases"
    __table_args__ = (
        # Explicit names: the uq_%(table_name)s_%(column_0_name)s convention
        # would name both of these after `entity_type` and collide on
        # PostgreSQL, where each UNIQUE constraint creates a same-named index.
        UniqueConstraint(
            "entity_type",
            "provider",
            "external_id",
            name="uq_entity_aliases_provider_external_id",
        ),
        UniqueConstraint(
            "entity_type",
            "provider",
            "sport_id",
            "normalized_alias",
            name="uq_entity_aliases_provider_sport_alias",
        ),
        Index("ix_entity_aliases_lookup", "entity_type", "provider", "normalized_alias"),
        enum_check("entity_type", EntityType),
        enum_check("source", AliasSource),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[EntityType] = mapped_column(db_enum(EntityType, "entity_type"))
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    sport_id: Mapped[int | None] = mapped_column(ForeignKey("sports.id"))
    provider: Mapped[str] = mapped_column(String(50))
    alias_name: Mapped[str | None] = mapped_column(String(200))
    normalized_alias: Mapped[str | None] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[AliasSource] = mapped_column(db_enum(AliasSource, "alias_source"))
    confidence: Mapped[float | None] = mapped_column(Float)  # fuzzy score / 100
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class ResolutionQueueItem(Base):
    """A provider name that could not be auto-resolved — surfaced in the admin
    screen with fuzzy-match candidates for a human decision."""

    __tablename__ = "resolution_queue"
    __table_args__ = (
        Index("ix_resolution_queue_status_created", "status", "created_at"),
        enum_check("entity_type", EntityType),
        enum_check("status", ResolutionStatus),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[EntityType] = mapped_column(db_enum(EntityType, "entity_type"))
    sport_id: Mapped[int | None] = mapped_column(ForeignKey("sports.id"))
    league_id: Mapped[int | None] = mapped_column(ForeignKey("leagues.id"))
    provider: Mapped[str] = mapped_column(String(50))
    raw_name: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(100))
    context: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    # Top fuzzy candidates: [{"entity_id": .., "name": .., "score": ..}, ...]
    candidates: Mapped[list[Any]] = mapped_column(JSONVariant, default=list)
    status: Mapped[ResolutionStatus] = mapped_column(
        db_enum(ResolutionStatus, "resolution_status"),
        default=ResolutionStatus.PENDING,
    )
    resolved_entity_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
