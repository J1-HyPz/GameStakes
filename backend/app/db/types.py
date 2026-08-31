"""Cross-backend column types.

JSONVariant stores JSONB on PostgreSQL and plain JSON (TEXT) on SQLite.

TZDateTime exists because SQLite has no timezone-aware storage: a
DateTime(timezone=True) column silently discards the offset, so an aware
non-UTC input is stored at the wrong instant and every read comes back naive —
while PostgreSQL returns aware UTC. Comparisons written against one backend
then break, or worse, quietly differ on the other. Everything is normalised to
UTC on the way in and returned aware on the way out.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

JSONVariant: TypeEngine[dict[str, Any] | list[Any]] = JSON().with_variant(JSONB(), "postgresql")


class TZDateTime(TypeDecorator[datetime]):
    """Timezone-safe timestamp: stores UTC everywhere, always reads back aware."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime rejected — pass an aware datetime "
                "(e.g. datetime.now(UTC)) so the instant is unambiguous"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)  # SQLite path; asyncpg is already aware
        return value
