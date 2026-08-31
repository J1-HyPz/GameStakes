"""Application settings, sourced from environment variables with sane defaults.

The app must start and run with zero configuration: SQLite database, no Redis,
no provider keys. Anything unavailable as a result is surfaced in the UI, never
a crash.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "GameStakes"
    environment: str = "production"
    log_level: str = "INFO"

    host: str = "0.0.0.0"  # noqa: S104 — single-container LAN app
    port: int = 8080
    root_path: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/gamestakes.db"
    # "default" (pooled) or "null" (fresh connection per checkout — used by the
    # test suite to avoid reusing connections across event loops).
    database_pool: str = "default"
    redis_url: str | None = None

    auth_enabled: bool = False
    auth_password: str | None = None
    jwt_secret: str | None = None

    # Directory holding the built SPA. Autodetected when unset (container
    # default /app/static, then ../frontend/dist for local dev).
    static_dir: Path | None = None
    # Writable volume for simulation draws and model artifacts.
    data_dir: Path = Path("./data")

    # Bankroll and staking. Bankroll is required before the builder will
    # recommend a stake — every number is expressed relative to it.
    bankroll: float = 0.0
    kelly_multiplier: float = 0.25
    daily_exposure_cap: float = 0.05  # share of bankroll
    weekly_exposure_cap: float = 0.15
    odds_format: str = "decimal"

    timezone: str = "Europe/London"
    currency: str = "GBP"

    # Ingestion cadence. Deliberately frugal: provider free tiers are small,
    # and The Odds API bills credits per market per region.
    scheduler_enabled: bool = True
    fixtures_refresh_hours: int = 6
    results_refresh_hours: int = 3
    # Odds cost credits per pull, so this is the stingiest schedule that still
    # gives a usable price history for closing line value.
    odds_refresh_hours: int = 8
    predictions_refresh_hours: int = 12

    # Provider keys (used from Phase 3). Empty string means "not configured".
    api_sports_key: str = ""
    the_odds_api_key: str = ""
    odds_region: str = "uk"
    football_data_org_key: str = ""
    thesportsdb_key: str = ""
    balldontlie_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
