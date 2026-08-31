"""Canonical database models.

Sport-agnostic core tables with sport-specific extension tables (e.g.
fighter_profiles, bout_results) so combat sports are first-class rather than
bolted onto a team-sport schema.

Importing this package registers every model on Base.metadata — Alembic's env
and the test fixtures rely on that.
"""

from app.db.models.betting import Bet, BetLeg, Settlement
from app.db.models.catalog import League, Season, Sport, Venue
from app.db.models.fixtures import BoutResult, Fixture, FixtureParticipant, Result
from app.db.models.ingest import EntityAlias, IngestJob, RawIngest, ResolutionQueueItem
from app.db.models.odds import OddsSnapshot
from app.db.models.participants import FighterProfile, Player, Team, TeamMembership
from app.db.models.predictions import ModelVersion, Prediction, Simulation
from app.db.models.stats import PlayerGameStats, TeamSeasonStats

__all__ = [
    "Bet",
    "BetLeg",
    "BoutResult",
    "EntityAlias",
    "FighterProfile",
    "Fixture",
    "FixtureParticipant",
    "IngestJob",
    "League",
    "ModelVersion",
    "OddsSnapshot",
    "Player",
    "PlayerGameStats",
    "Prediction",
    "RawIngest",
    "ResolutionQueueItem",
    "Result",
    "Season",
    "Settlement",
    "Simulation",
    "Sport",
    "Team",
    "TeamMembership",
    "TeamSeasonStats",
    "Venue",
]
