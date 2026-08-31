"""Model versions, simulations and predictions.

Reproducibility contract: a prediction row records its model version, the RNG
seed (via its simulation), the input feature hash and the generation
timestamp. Same inputs + seed + version => identical prediction, and results
are attributable to a specific model version.

Raw simulation draws are persisted as compressed artifacts on the /data volume
(simulations.artifact_path) with summary stats inline — parlay pricing must
come from the joint distribution, and 20k draws per fixture do not belong in
table rows.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import Confidence, db_enum, enum_check
from app.db.types import JSONVariant, TZDateTime


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))  # e.g. "football-dixon-coles-elo"
    version: Mapped[str] = mapped_column(String(40))
    hyperparameters: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    training_window_start: Mapped[date | None] = mapped_column(Date)
    training_window_end: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class Simulation(Base):
    __tablename__ = "simulations"

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"))
    seed: Mapped[int] = mapped_column(BigInteger)
    n_iterations: Mapped[int] = mapped_column(Integer)
    # Compressed draws (npz) under /data; None if draws were pruned.
    artifact_path: Mapped[str | None] = mapped_column(String(300))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        Index("ix_predictions_fixture_market", "fixture_id", "market"),
        Index("ix_predictions_generated_at", "generated_at"),
        enum_check("confidence", Confidence),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id"), index=True)
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"))
    simulation_id: Mapped[int | None] = mapped_column(ForeignKey("simulations.id"))
    market: Mapped[str] = mapped_column(String(80))
    selection: Mapped[str] = mapped_column(String(120))
    line: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))  # props
    probability: Mapped[float] = mapped_column(Float)
    fair_price_decimal: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))  # 1/probability
    confidence: Mapped[Confidence] = mapped_column(db_enum(Confidence, "confidence"))
    confidence_score: Mapped[float | None] = mapped_column(Float)  # 0..1 data-quality score
    feature_hash: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(TZDateTime)
    # Interval bounds and any per-market detail.
    extra: Mapped[dict[str, Any]] = mapped_column(JSONVariant, default=dict)

    simulation: Mapped[Simulation | None] = relationship()
