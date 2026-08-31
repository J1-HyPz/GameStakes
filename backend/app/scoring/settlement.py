"""Grading predictions and bet legs against real outcomes.

Half-win and half-lose exist because quarter-line Asian handicaps split the
stake across two lines; pushes and voids exist because a postponed fixture is
not a loss. Getting these wrong quietly inflates or deflates every metric
downstream, so each is handled explicitly rather than collapsed into win/lose.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import (
    BetStatus,
    FixtureStatus,
    Outcome,
    SettlementMethod,
    Side,
)
from app.db.models import Bet, BetLeg, Fixture, Prediction, Result, Settlement

log = get_logger(__name__)


def grade_selection(
    market: str,
    selection: str,
    line: Decimal | None,
    home_score: int,
    away_score: int,
) -> Outcome:
    """Grade one selection against a final score."""
    total = home_score + away_score
    margin = home_score - away_score
    float_line = float(line) if line is not None else None

    if market == "1x2":
        winner = "home" if margin > 0 else "away" if margin < 0 else "draw"
        return Outcome.WIN if selection == winner else Outcome.LOSE

    if market == "double_chance":
        won = {
            "home_or_draw": margin >= 0,
            "away_or_draw": margin <= 0,
            "home_or_away": margin != 0,
        }.get(selection)
        return Outcome.WIN if won else Outcome.LOSE

    if market == "totals" and float_line is not None:
        if total == float_line:
            return Outcome.PUSH
        over = total > float_line
        return Outcome.WIN if (selection == "over") == over else Outcome.LOSE

    if market == "btts":
        both = home_score > 0 and away_score > 0
        return Outcome.WIN if (selection == "yes") == both else Outcome.LOSE

    if market == "clean_sheet":
        kept = away_score == 0 if selection == "home" else home_score == 0
        return Outcome.WIN if kept else Outcome.LOSE

    if market in {"handicap", "spreads"} and float_line is not None:
        return _grade_handicap(selection, float_line, margin)

    if market == "team_total_home" and float_line is not None:
        return _grade_over_under(selection, home_score, float_line)
    if market == "team_total_away" and float_line is not None:
        return _grade_over_under(selection, away_score, float_line)

    if market == "correct_score":
        return Outcome.WIN if selection == f"{home_score}-{away_score}" else Outcome.LOSE

    log.warning("settlement.unknown_market", market=market, selection=selection)
    return Outcome.VOID


def _grade_over_under(selection: str, value: int, line: float) -> Outcome:
    if value == line:
        return Outcome.PUSH
    over = value > line
    return Outcome.WIN if (selection == "over") == over else Outcome.LOSE


def _grade_handicap(selection: str, line: float, margin: int) -> Outcome:
    """Asian handicaps, including quarter lines that split the stake."""
    adjusted = margin + line if selection == "home" else -margin + line

    if abs(line * 4) % 2 == 1:  # quarter line (.25 / .75): stake splits
        lower, upper = line - 0.25, line + 0.25
        outcomes = [
            _grade_handicap(selection, lower, margin),
            _grade_handicap(selection, upper, margin),
        ]
        wins = outcomes.count(Outcome.WIN)
        pushes = outcomes.count(Outcome.PUSH)
        if wins == 2:
            return Outcome.WIN
        if wins == 1 and pushes == 1:
            return Outcome.HALF_WIN
        if pushes == 2:
            return Outcome.PUSH
        if wins == 0 and pushes == 1:
            return Outcome.HALF_LOSE
        return Outcome.LOSE

    if adjusted == 0:
        return Outcome.PUSH
    return Outcome.WIN if adjusted > 0 else Outcome.LOSE


def payout_multiplier(outcome: Outcome, decimal_odds: float) -> float:
    """Returned stake per unit, including the stake itself."""
    return {
        Outcome.WIN: decimal_odds,
        Outcome.HALF_WIN: 1 + (decimal_odds - 1) / 2,
        Outcome.PUSH: 1.0,
        Outcome.VOID: 1.0,
        Outcome.HALF_LOSE: 0.5,
        Outcome.LOSE: 0.0,
    }[outcome]


class Settler:
    """Grades open predictions and bets whose fixtures have finished."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def settle_all(self) -> dict[str, int]:
        predictions = await self._settle_predictions()
        bets = await self._settle_bets()
        await self.session.commit()
        return {"predictions": predictions, "bets": bets}

    async def _settle_predictions(self) -> int:
        """Grade every prediction whose fixture has a final result.

        Predictions are graded whether or not they were bet on — calibration
        needs the full sample, not just the ones that looked attractive.
        """
        already = set(
            (
                await self.session.execute(
                    select(Settlement.prediction_id).where(Settlement.prediction_id.is_not(None))
                )
            )
            .scalars()
            .all()
        )

        stmt = (
            select(Prediction, Result)
            .join(Fixture, Prediction.fixture_id == Fixture.id)
            .join(Result, Result.fixture_id == Fixture.id)
            .where(Fixture.status == FixtureStatus.FINISHED)
        )
        rows = (await self.session.execute(stmt)).all()

        graded = 0
        for prediction, result in rows:
            if prediction.id in already:
                continue
            if result.home_score is None or result.away_score is None:
                continue
            outcome = grade_selection(
                prediction.market,
                prediction.selection,
                prediction.line,
                result.home_score,
                result.away_score,
            )
            self.session.add(
                Settlement(
                    prediction_id=prediction.id,
                    outcome=outcome,
                    method=SettlementMethod.AUTO,
                    graded_at=datetime.now(UTC),
                    detail={"score": [result.home_score, result.away_score]},
                )
            )
            graded += 1
        return graded

    async def _settle_bets(self) -> int:
        bets = (
            (await self.session.execute(select(Bet).where(Bet.status == BetStatus.PLACED)))
            .scalars()
            .all()
        )

        settled = 0
        for bet in bets:
            legs = (
                (await self.session.execute(select(BetLeg).where(BetLeg.bet_id == bet.id)))
                .scalars()
                .all()
            )
            if not legs:
                continue

            outcomes = []
            for leg in legs:
                outcome = await self._grade_leg(leg)
                if outcome is None:
                    break  # a leg is unresolved; the bet stays open
                outcomes.append(outcome)
            else:
                bet.payout = _bet_payout(bet.stake, legs, outcomes)
                bet.status = (
                    BetStatus.VOID
                    if all(o in {Outcome.VOID, Outcome.PUSH} for o in outcomes)
                    else BetStatus.SETTLED
                )
                bet.settled_at = datetime.now(UTC)
                settled += 1
        return settled

    async def _grade_leg(self, leg: BetLeg) -> Outcome | None:
        if leg.result is not None:
            return leg.result

        fixture = await self.session.get(Fixture, leg.fixture_id)
        if fixture is None:
            return None
        if fixture.status in {FixtureStatus.POSTPONED, FixtureStatus.CANCELLED}:
            leg.result = Outcome.VOID
            self._record(leg, Outcome.VOID, {"reason": fixture.status.value})
            return Outcome.VOID
        if fixture.status != FixtureStatus.FINISHED:
            return None

        result = (
            await self.session.execute(select(Result).where(Result.fixture_id == fixture.id))
        ).scalar_one_or_none()
        if result is None or result.home_score is None or result.away_score is None:
            return None

        outcome = grade_selection(
            leg.market, leg.selection, leg.line, result.home_score, result.away_score
        )
        leg.result = outcome
        self._record(leg, outcome, {"score": [result.home_score, result.away_score]})
        return outcome

    def _record(self, leg: BetLeg, outcome: Outcome, detail: dict[str, Any]) -> None:
        self.session.add(
            Settlement(
                bet_leg_id=leg.id,
                outcome=outcome,
                method=SettlementMethod.AUTO,
                graded_at=datetime.now(UTC),
                detail=detail,
            )
        )

    async def override(self, bet_leg_id: int, outcome: Outcome, note: str = "") -> BetLeg:
        """Manual grading for the cases automation cannot reach."""
        leg = await self.session.get(BetLeg, bet_leg_id)
        if leg is None:
            raise ValueError(f"bet leg {bet_leg_id} not found")
        leg.result = outcome
        self.session.add(
            Settlement(
                bet_leg_id=leg.id,
                outcome=outcome,
                method=SettlementMethod.MANUAL,
                graded_at=datetime.now(UTC),
                detail={"note": note},
            )
        )
        await self.session.commit()
        return leg


def _bet_payout(stake: Decimal, legs: Sequence[BetLeg], outcomes: list[Outcome]) -> Decimal:
    """Parlay payout: multipliers compound, and one loss makes it zero."""
    multiplier = 1.0
    for leg, outcome in zip(legs, outcomes, strict=True):
        multiplier *= payout_multiplier(outcome, float(leg.price_decimal))
    return (stake * Decimal(str(multiplier))).quantize(Decimal("0.01"))


def winner_side(home_score: int, away_score: int) -> Side | None:
    if home_score > away_score:
        return Side.HOME
    if away_score > home_score:
        return Side.AWAY
    return None
