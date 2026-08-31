"""Fixtures and results.

A fixture is any scheduled contest: a match for team sports, a bout for combat
sports. Participants live in fixture_participants (team_id XOR player_id), so
combat sports are first-class. Combat-specific result detail extends the
generic result via bout_results.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import FixtureStatus, Side, VictoryMethod, db_enum, enum_check
from app.db.models._mixins import TimestampMixin
from app.db.types import JSONVariant, TZDateTime


class Fixture(Base, TimestampMixin):
    __tablename__ = "fixtures"
    __table_args__ = (
        Index("ix_fixtures_league_id_start_time", "league_id", "start_time"),
        enum_check("status", FixtureStatus),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"), index=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"))
    start_time: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    status: Mapped[FixtureStatus] = mapped_column(
        db_enum(FixtureStatus, "fixture_status"),
        default=FixtureStatus.SCHEDULED,
        index=True,
    )
    round: Mapped[str | None] = mapped_column(String(80))  # "Matchday 12", "Week 5", "QF"
    event_name: Mapped[str | None] = mapped_column(String(150))  # combat card, e.g. "UFC 300"
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venues.id"))
    neutral_site: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduled_rounds: Mapped[int | None] = mapped_column(Integer)  # combat: 3, 5 or 12

    participants: Mapped[list["FixtureParticipant"]] = relationship(back_populates="fixture")
    result: Mapped["Result | None"] = relationship(back_populates="fixture")


class FixtureParticipant(Base):
    __tablename__ = "fixture_participants"
    __table_args__ = (
        UniqueConstraint("fixture_id", "side"),
        CheckConstraint(
            "(team_id IS NULL) != (player_id IS NULL)",
            name="exactly_one_participant_kind",
        ),
        enum_check("side", Side),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    side: Mapped[Side] = mapped_column(db_enum(Side, "side"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), index=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), index=True)

    fixture: Mapped[Fixture] = relationship(back_populates="participants")


class Result(Base, TimestampMixin):
    __tablename__ = "results"
    __table_args__ = (enum_check("winner_side", Side),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), unique=True)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    winner_side: Mapped[Side | None] = mapped_column(db_enum(Side, "side"))  # NULL = draw
    # Period/half breakdowns, OT/shootout detail, HT score — per-sport shapes.
    score_detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    finalized_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    fixture: Mapped[Fixture] = relationship(back_populates="result")
    bout_result: Mapped["BoutResult | None"] = relationship(back_populates="result")


class BoutResult(Base):
    """Combat-sport extension: how and when the bout ended — the basis for
    method and round markets."""

    __tablename__ = "bout_results"
    __table_args__ = (enum_check("method", VictoryMethod),)

    result_id: Mapped[int] = mapped_column(ForeignKey("results.id"), primary_key=True)
    method: Mapped[VictoryMethod] = mapped_column(db_enum(VictoryMethod, "victory_method"))
    end_round: Mapped[int | None] = mapped_column(Integer)
    end_time_seconds: Mapped[int | None] = mapped_column(Integer)  # into the final round
    detail: Mapped[str | None] = mapped_column(String(200))  # e.g. "rear-naked choke"

    result: Mapped[Result] = relationship(back_populates="bout_result")
