"""Sports, leagues, seasons and venues — seeded from YAML, not hardcoded."""

from datetime import date
from typing import Any

from sqlalchemy import Boolean, Date, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import CompetitionType, SportKind, db_enum, enum_check
from app.db.models._mixins import TimestampMixin
from app.db.types import JSONVariant


class Sport(Base, TimestampMixin):
    __tablename__ = "sports"
    __table_args__ = (enum_check("kind", SportKind),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[SportKind] = mapped_column(db_enum(SportKind, "sport_kind"))

    leagues: Mapped[list["League"]] = relationship(back_populates="sport")


class League(Base, TimestampMixin):
    """A competition: league, cup, international tournament, or combat-sport
    organisation (UFC, PFL, ...). Boxing uses an org-agnostic umbrella league
    since it is fighter-centric rather than league-centric."""

    __tablename__ = "leagues"
    __table_args__ = (enum_check("competition_type", CompetitionType),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"), index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(150))
    short_name: Mapped[str | None] = mapped_column(String(50))
    country: Mapped[str | None] = mapped_column(String(80))
    competition_type: Mapped[CompetitionType] = mapped_column(
        db_enum(CompetitionType, "competition_type")
    )
    tier: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-league model knobs (e.g. form decay half-life overrides).
    config: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    sport: Mapped[Sport] = relationship(back_populates="leagues")
    seasons: Mapped[list["Season"]] = relationship(back_populates="league")


class Season(Base, TimestampMixin):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("league_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    name: Mapped[str] = mapped_column(String(50))  # e.g. "2025/26", "2026"
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)

    league: Mapped[League] = relationship(back_populates="seasons")


class Venue(Base, TimestampMixin):
    """Structured venue data feeds weather/altitude adjustments later
    (wind for NFL totals, altitude for Denver)."""

    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), index=True)
    city: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(80))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    altitude_m: Mapped[int | None] = mapped_column(Integer)
    is_indoor: Mapped[bool | None] = mapped_column(Boolean)
