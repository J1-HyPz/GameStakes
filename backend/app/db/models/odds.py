"""Bookmaker odds as a time-series.

Every capture is a new row — never an update — because closing line value
needs the price history, not just the latest price.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import TZDateTime


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    __table_args__ = (
        Index("ix_odds_snapshots_fixture_market_time", "fixture_id", "market", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    bookmaker: Mapped[str] = mapped_column(String(80))
    market: Mapped[str] = mapped_column(String(80))  # e.g. "1x2", "totals", "player_points"
    selection: Mapped[str] = mapped_column(String(120))  # e.g. "home", "over", player name
    line: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))  # 2.5, -1.75, 250.5
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))  # props
    price_decimal: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    provider: Mapped[str] = mapped_column(String(50))
    captured_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
