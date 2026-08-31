"""Team and player statistics.

team_season_stats rows carry an as_of date so models (and the backtester) can
select the snapshot that was current before kickoff — the no-lookahead rule
depends on this. Sport-specific metrics live in the JSON `extra`/`stats`
payloads; the modelling layer owns their schemas per sport.
"""

from datetime import date
from typing import Any

from sqlalchemy import Date, Float, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._mixins import TimestampMixin
from app.db.types import JSONVariant


class TeamSeasonStats(Base, TimestampMixin):
    __tablename__ = "team_season_stats"
    __table_args__ = (UniqueConstraint("team_id", "season_id", "as_of"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    played: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    draws: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    score_for: Mapped[float | None] = mapped_column(Float)  # goals/points scored
    score_against: Mapped[float | None] = mapped_column(Float)
    # Sport-specific: xG, pace, EPA splits, four factors, ...
    extra: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)


class PlayerGameStats(Base, TimestampMixin):
    __tablename__ = "player_game_stats"
    __table_args__ = (UniqueConstraint("player_id", "fixture_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    minutes: Mapped[float | None] = mapped_column(Float)
    # Per-sport counting stats: shots, targets, carries, rebounds, sig. strikes...
    stats: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
