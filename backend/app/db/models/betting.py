"""Bets, legs and settlement ledger."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import (
    BetStatus,
    BetTier,
    Outcome,
    SettlementMethod,
    db_enum,
    enum_check,
)
from app.db.models._mixins import TimestampMixin
from app.db.types import JSONVariant, TZDateTime


class Bet(Base, TimestampMixin):
    __tablename__ = "bets"
    __table_args__ = (
        enum_check("tier", BetTier),
        enum_check("status", BetStatus),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tier: Mapped[BetTier] = mapped_column(db_enum(BetTier, "bet_tier"))
    status: Mapped[BetStatus] = mapped_column(
        db_enum(BetStatus, "bet_status"), default=BetStatus.PENDING, index=True
    )
    stake: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    # Combined price from correlated simulation; naive = independence assumption,
    # stored so the correlation effect stays visible.
    combined_price_decimal: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    combined_probability: Mapped[float] = mapped_column(Float)
    naive_probability: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float | None] = mapped_column(Float)
    kelly_fraction: Mapped[float | None] = mapped_column(Float)
    bankroll_at_placement: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    placed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    settled_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    payout: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    notes: Mapped[str | None] = mapped_column(Text)

    legs: Mapped[list["BetLeg"]] = relationship(back_populates="bet")


class BetLeg(Base, TimestampMixin):
    __tablename__ = "bet_legs"
    __table_args__ = (enum_check("result", Outcome),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bet_id: Mapped[int] = mapped_column(ForeignKey("bets.id"), index=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    market: Mapped[str] = mapped_column(String(80))
    selection: Mapped[str] = mapped_column(String(120))
    line: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    price_decimal: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    bookmaker: Mapped[str | None] = mapped_column(String(80))
    model_probability: Mapped[float | None] = mapped_column(Float)
    implied_probability: Mapped[float | None] = mapped_column(Float)  # de-vigged
    edge: Mapped[float | None] = mapped_column(Float)
    result: Mapped[Outcome | None] = mapped_column(db_enum(Outcome, "outcome"))

    bet: Mapped[Bet] = relationship(back_populates="legs")


class Settlement(Base):
    """Audit ledger of grading events for predictions and bet legs — one row
    per grading action, including manual overrides."""

    __tablename__ = "settlements"
    __table_args__ = (
        CheckConstraint(
            "(prediction_id IS NULL) != (bet_leg_id IS NULL)",
            name="exactly_one_subject",
        ),
        enum_check("outcome", Outcome),
        enum_check("method", SettlementMethod),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"), index=True)
    bet_leg_id: Mapped[int | None] = mapped_column(ForeignKey("bet_legs.id"), index=True)
    outcome: Mapped[Outcome] = mapped_column(db_enum(Outcome, "outcome"))
    method: Mapped[SettlementMethod] = mapped_column(db_enum(SettlementMethod, "settlement_method"))
    graded_at: Mapped[datetime] = mapped_column(TZDateTime)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
