"""Bankroll ledger and exposure limits.

The builder refuses to exceed the daily and weekly caps. This is a product
feature rather than a warning: a cap that can be talked past is not a cap, and
the moment that matters is the one where several tempting bets land on the
same day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.enums import BetStatus
from app.db.models import Bet


@dataclass(frozen=True)
class ExposureSummary:
    bankroll: Decimal
    staked_today: Decimal
    staked_this_week: Decimal
    remaining_daily: Decimal
    remaining_weekly: Decimal
    open_bets: int
    open_stake: Decimal

    @property
    def daily_cap_reached(self) -> bool:
        return self.remaining_daily <= 0

    @property
    def weekly_cap_reached(self) -> bool:
        return self.remaining_weekly <= 0

    def describe(self) -> str:
        if self.daily_cap_reached:
            return "Daily exposure cap reached — no further bets today."
        if self.weekly_cap_reached:
            return "Weekly exposure cap reached — no further bets this week."
        return f"{self.remaining_daily} of today's exposure remains ({self.open_bets} bets open)."


async def exposure_summary(
    session: AsyncSession, bankroll: Decimal | None = None
) -> ExposureSummary:
    settings = get_settings()
    bankroll = bankroll if bankroll is not None else Decimal(str(settings.bankroll))
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())

    staked_today = await _staked_since(session, day_start)
    staked_week = await _staked_since(session, week_start)

    open_stmt = select(func.count(Bet.id), func.coalesce(func.sum(Bet.stake), 0)).where(
        Bet.status == BetStatus.PLACED
    )
    open_count, open_stake = (await session.execute(open_stmt)).one()

    daily_cap = bankroll * Decimal(str(settings.daily_exposure_cap))
    weekly_cap = bankroll * Decimal(str(settings.weekly_exposure_cap))

    return ExposureSummary(
        bankroll=bankroll,
        staked_today=staked_today,
        staked_this_week=staked_week,
        remaining_daily=max(daily_cap - staked_today, Decimal("0")),
        remaining_weekly=max(weekly_cap - staked_week, Decimal("0")),
        open_bets=int(open_count or 0),
        open_stake=Decimal(str(open_stake or 0)),
    )


async def _staked_since(session: AsyncSession, since: datetime) -> Decimal:
    stmt = select(func.coalesce(func.sum(Bet.stake), 0)).where(
        Bet.placed_at.is_not(None),
        Bet.placed_at >= since,
        Bet.status.in_([BetStatus.PLACED, BetStatus.SETTLED]),
    )
    total = (await session.execute(stmt)).scalar_one()
    return Decimal(str(total or 0))
