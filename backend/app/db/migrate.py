"""Run Alembic migrations programmatically.

Used by the container entrypoint (`python -m app.db.migrate`) so migrations
work from an installed package without depending on alembic.ini's location.
Runs in its own process, so the async migration env gets a clean event loop.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger


def build_alembic_config(database_url: str | None = None) -> Config:
    settings = get_settings()
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "alembic"))
    # Escape % for configparser interpolation — passwords legally contain it.
    url = database_url or settings.database_url
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade_to_head() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.environment)
    log = get_logger(__name__)
    log.info("migrations.start", database=settings.database_url.split("@")[-1])
    command.upgrade(build_alembic_config(), "head")
    log.info("migrations.complete")


if __name__ == "__main__":
    upgrade_to_head()
