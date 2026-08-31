"""Teams, players and combat-sport fighter extensions.

normalized_name columns are maintained by the entity-resolution service
(app.ingest.resolution.normalize_name) and are the lookup key for matching
provider names to canonical entities.
"""

from datetime import date
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models._mixins import TimestampMixin
from app.db.types import JSONVariant


class Team(Base, TimestampMixin):
    __tablename__ = "teams"
    __table_args__ = (Index("ix_teams_sport_id_normalized_name", "sport_id", "normalized_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    normalized_name: Mapped[str] = mapped_column(String(150))
    short_name: Mapped[str | None] = mapped_column(String(50))
    code: Mapped[str | None] = mapped_column(String(10))  # e.g. "MUN", "KC"
    country: Mapped[str | None] = mapped_column(String(80))
    logo_url: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Player(Base, TimestampMixin):
    """A player or an individual combat-sport fighter."""

    __tablename__ = "players"
    __table_args__ = (Index("ix_players_sport_id_normalized_name", "sport_id", "normalized_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    normalized_name: Mapped[str] = mapped_column(String(150))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    country: Mapped[str | None] = mapped_column(String(80))
    position: Mapped[str | None] = mapped_column(String(50))
    height_cm: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    fighter_profile: Mapped["FighterProfile | None"] = relationship(back_populates="player")


class FighterProfile(Base, TimestampMixin):
    """Combat-sport extension: physical/style attributes the fight models use."""

    __tablename__ = "fighter_profiles"

    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    reach_cm: Mapped[int | None] = mapped_column(Integer)
    stance: Mapped[str | None] = mapped_column(String(30))  # orthodox/southpaw/switch
    weight_class: Mapped[str | None] = mapped_column(String(50))
    wins: Mapped[int | None] = mapped_column(Integer)
    losses: Mapped[int | None] = mapped_column(Integer)
    draws: Mapped[int | None] = mapped_column(Integer)
    no_contests: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    player: Mapped[Player] = relationship(back_populates="fighter_profile")


class TeamMembership(Base, TimestampMixin):
    """Which team a player belongs to in a given season — needed to project
    player props from team context."""

    __tablename__ = "team_memberships"
    __table_args__ = (UniqueConstraint("player_id", "team_id", "season_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"))
    shirt_number: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[str | None] = mapped_column(String(50))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
