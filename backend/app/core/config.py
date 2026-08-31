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
    redis_url: str | None = None

    auth_enabled: bool = False
    auth_password: str | None = None
    jwt_secret: str | None = None

    # Directory holding the built SPA. Autodetected when unset (container
    # default /app/static, then ../frontend/dist for local dev).
    static_dir: Path | None = None

    timezone: str = "Europe/London"
    currency: str = "GBP"

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
