"""Backtesting: the no-lookahead guarantee above all.

A backtest that has seen the result it predicts looks extraordinary and loses
money live, so the guard is tested directly rather than assumed.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import FixtureStatus, Side
from app.db.models import (
    Fixture,
    FixtureParticipant,
    League,
    OddsSnapshot,
    Result,
    Sport,
    Team,
)
from app.models import football
from app.scoring.backtest import (
    LookaheadError,
    _assert_no_lookahead,
    _prices_before_kickoff,
    compare_results,
    run_backtest,
)
from app.scoring.metrics import Interval


class TestLookaheadGuard:
    def test_training_data_from_after_kickoff_is_rejected(self) -> None:
        kick_off = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
        contaminated = [football.Match(1, 2, 1, 0, date(2026, 3, 5))]

        with pytest.raises(LookaheadError, match="at or after"):
            _assert_no_lookahead(contaminated, kick_off)

    def test_a_match_on_the_same_day_is_also_rejected(self) -> None:
        """Same-day is not safe: the fixture may already have finished."""
        kick_off = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
        with pytest.raises(LookaheadError):
            _assert_no_lookahead([football.Match(1, 2, 1, 0, date(2026, 3, 1))], kick_off)

    def test_strictly_earlier_training_data_passes(self) -> None:
        kick_off = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
        _assert_no_lookahead([football.Match(1, 2, 1, 0, date(2026, 2, 28))], kick_off)


async def _seed_league(session: AsyncSession, slug: str = "premier-league") -> tuple[League, int]:
    league = (await session.execute(select(League).where(League.slug == slug))).scalar_one()
    sport = await session.get(Sport, league.sport_id)
    assert sport is not None
    return league, sport.id


async def _make_fixture(
    session: AsyncSession,
    league: League,
    sport_id: int,
    home: Team,
    away: Team,
    kick_off: datetime,
    home_score: int,
    away_score: int,
) -> Fixture:
    fixture = Fixture(
        sport_id=sport_id,
        league_id=league.id,
        start_time=kick_off,
        status=FixtureStatus.FINISHED,
    )
    session.add(fixture)
    await session.flush()
    session.add_all(
        [
            FixtureParticipant(fixture_id=fixture.id, side=Side.HOME, team_id=home.id),
            FixtureParticipant(fixture_id=fixture.id, side=Side.AWAY, team_id=away.id),
        ]
    )
    session.add(
        Result(
            fixture_id=fixture.id,
            home_score=home_score,
            away_score=away_score,
            winner_side=(
                Side.HOME
                if home_score > away_score
                else Side.AWAY
                if away_score > home_score
                else None
            ),
        )
    )
    await session.flush()
    return fixture


class TestPriceSelection:
    async def test_only_prices_captured_before_kickoff_are_used(
        self, db_session: AsyncSession
    ) -> None:
        """The subtlest way a backtest lies: using the settled price."""
        league, sport_id = await _seed_league(db_session)
        home = Team(sport_id=sport_id, name="Price Home", normalized_name="price home")
        away = Team(sport_id=sport_id, name="Price Away", normalized_name="price away")
        db_session.add_all([home, away])
        await db_session.flush()

        kick_off = datetime(2026, 5, 1, 15, 0, tzinfo=UTC)
        fixture = await _make_fixture(db_session, league, sport_id, home, away, kick_off, 2, 1)
        db_session.add_all(
            [
                OddsSnapshot(
                    fixture_id=fixture.id,
                    bookmaker="book",
                    market="1x2",
                    selection="home",
                    price_decimal=Decimal("2.00"),
                    provider="test",
                    captured_at=kick_off - timedelta(hours=2),
                ),
                # Captured after kickoff — must never be used.
                OddsSnapshot(
                    fixture_id=fixture.id,
                    bookmaker="book",
                    market="1x2",
                    selection="home",
                    price_decimal=Decimal("1.01"),
                    provider="test",
                    captured_at=kick_off + timedelta(minutes=30),
                ),
            ]
        )
        await db_session.flush()

        prices = await _prices_before_kickoff(db_session, fixture.id, kick_off)

        assert prices[("1x2", None)]["home"] == 2.00
        await db_session.rollback()


class TestBacktestRun:
    async def test_insufficient_history_is_reported_not_faked(
        self, db_session: AsyncSession
    ) -> None:
        """With too little data the honest output is 'skipped', not a number."""
        result = await run_backtest(
            db_session,
            "championship",
            date(2026, 3, 1),
            date(2026, 3, 15),
            min_training_matches=1000,
        )

        assert result.bets_placed == 0
        assert result.windows >= 1
        assert any("not enough to fit" in note for note in result.notes)

    async def test_unknown_league_raises(self, db_session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="unknown league"):
            await run_backtest(db_session, "not-a-league", date(2026, 1, 1), date(2026, 2, 1))

    async def test_no_qualifying_bets_is_stated_as_a_real_result(
        self, db_session: AsyncSession
    ) -> None:
        result = await run_backtest(db_session, "la-liga", date(2026, 3, 1), date(2026, 3, 8))
        assert result.bets_placed == 0
        assert result.notes


class TestModelComparison:
    def test_overlapping_intervals_are_called_indistinguishable(self) -> None:
        """Two ROIs differing by a few points over a hundred bets are not a
        ranking, and saying so prevents chasing noise."""
        a = _result_with_roi(Interval(0.05, -0.02, 0.12, 120))
        b = _result_with_roi(Interval(0.02, -0.05, 0.09, 120))

        verdict = compare_results(a, b)

        assert verdict["intervals_overlap"] is True
        assert "indistinguishable" in str(verdict["verdict"])

    def test_separated_intervals_produce_a_verdict(self) -> None:
        a = _result_with_roi(Interval(0.20, 0.15, 0.25, 500))
        b = _result_with_roi(Interval(-0.05, -0.10, -0.01, 500))

        verdict = compare_results(a, b)

        assert verdict["intervals_overlap"] is False
        assert verdict["verdict"] == "A performed better"


def _result_with_roi(roi: Interval):  # type: ignore[no-untyped-def]
    from app.scoring.backtest import BacktestResult

    return BacktestResult(
        league="premier-league",
        model_version="1.0.0",
        start=date(2026, 1, 1),
        end=date(2026, 6, 1),
        windows=20,
        fixtures_predicted=200,
        bets_placed=roi.n,
        hit_rate=Interval(0.5, 0.4, 0.6, roi.n),
        roi=roi,
        brier_score=0.22,
        log_loss=0.65,
        calibration=[],
        final_bankroll=1000.0,
        max_drawdown=0.1,
        longest_losing_streak=4,
    )
