"""Domain enums shared by the database layer, services and API.

Stored as VARCHAR with the enum *values* persisted (native_enum=False), so the
same DDL works on PostgreSQL and SQLite and rows stay readable.

Value legality is enforced at the database level by an explicit CHECK per enum
column — see `enum_check`, attached in each model's __table_args__. The
SQLAlchemy Enum type alone would not do this: its own constraint is disabled by
default, and enabling it produces type-bound CHECKs that `alembic check`
reports as permanent false drift. Explicit constraints compare cleanly and
carry deterministic names from the naming convention.

Adding an enum member therefore needs a migration that drops and recreates the
column's CHECK (use batch_alter_table so SQLite works too).
"""

import enum

from sqlalchemy import CheckConstraint
from sqlalchemy import Enum as SAEnum


class SportKind(enum.StrEnum):
    TEAM = "team"
    COMBAT = "combat"


class CompetitionType(enum.StrEnum):
    LEAGUE = "league"
    CUP = "cup"
    INTERNATIONAL = "international"
    ORGANISATION = "organisation"  # combat-sport promotions (UFC, PFL, ...)


class FixtureStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    IN_PLAY = "in_play"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Side(enum.StrEnum):
    """Fixture participant side. For combat sports HOME is the first-listed
    fighter (red corner / champion side), AWAY the second."""

    HOME = "home"
    AWAY = "away"


class VictoryMethod(enum.StrEnum):
    KO_TKO = "ko_tko"
    SUBMISSION = "submission"
    DECISION_UNANIMOUS = "decision_unanimous"
    DECISION_SPLIT = "decision_split"
    DECISION_MAJORITY = "decision_majority"
    TECHNICAL_DECISION = "technical_decision"
    DRAW = "draw"
    NO_CONTEST = "no_contest"
    DISQUALIFICATION = "disqualification"


class Confidence(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class BetTier(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MANUAL = "manual"


class BetStatus(enum.StrEnum):
    PENDING = "pending"
    PLACED = "placed"
    SETTLED = "settled"
    VOID = "void"


class Outcome(enum.StrEnum):
    """Grading outcome for a prediction or bet leg. Half outcomes cover
    quarter-line Asian handicaps."""

    WIN = "win"
    LOSE = "lose"
    PUSH = "push"
    VOID = "void"
    HALF_WIN = "half_win"
    HALF_LOSE = "half_lose"


class SettlementMethod(enum.StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class JobStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"


class EntityType(enum.StrEnum):
    TEAM = "team"
    PLAYER = "player"
    LEAGUE = "league"
    FIXTURE = "fixture"


class AliasSource(enum.StrEnum):
    PROVIDER_ID = "provider_id"  # matched via the provider's stable external id
    EXACT = "exact"  # normalized names matched exactly
    FUZZY = "fuzzy"  # accepted by fuzzy matching above threshold
    MANUAL = "manual"  # resolved by the user in the admin screen


class ResolutionStatus(enum.StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    IGNORED = "ignored"


def db_enum(enum_cls: type[enum.StrEnum], name: str) -> SAEnum:
    """VARCHAR-backed enum column storing the enum values, portable across
    PostgreSQL and SQLite."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
    )


def enum_check(column: str, enum_cls: type[enum.StrEnum]) -> CheckConstraint:
    """CHECK constraint restricting `column` to the enum's values.

    Guards write paths that bypass the ORM (bulk loads, raw SQL fixes), where
    an invalid string would otherwise persist silently and only surface as a
    LookupError when something later reads it.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return CheckConstraint(f"{column} IN ({values})", name=f"{column}_valid")
