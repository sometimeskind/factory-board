"""Settings, read from the environment once at start-up."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# A source checkout keeps migrations/ beside the package. The image installs the package
# into a venv, so its Dockerfile sets FACTORY_BOARD_MIGRATIONS_DIR instead.
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class ConfigError(Exception):
    """A setting is missing or unusable."""


SERVE_URL_VAR = "FACTORY_BOARD_DATABASE_URL"
MIGRATE_URL_VAR = "FACTORY_BOARD_MIGRATE_DATABASE_URL"


@dataclass(frozen=True)
class Settings:
    """The two database URLs are for two roles, and a process needs only its own.

    The server connects as board_app, which can do nothing until it takes a tier. The
    migrations connect as the role that owns the tables. Neither URL is read at start-up:
    ask for the one the command needs, and it fails by name if it is missing.
    """

    database_url: str
    migrate_database_url: str
    migrations_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        migrations_dir = env.get("FACTORY_BOARD_MIGRATIONS_DIR", "")
        return cls(
            database_url=env.get(SERVE_URL_VAR, ""),
            migrate_database_url=env.get(MIGRATE_URL_VAR, ""),
            migrations_dir=Path(migrations_dir) if migrations_dir else DEFAULT_MIGRATIONS_DIR,
        )

    def serve_url(self) -> str:
        """The URL the store connects with, as board_app."""
        if not self.database_url:
            raise ConfigError(f"{SERVE_URL_VAR} is not set")
        return self.database_url

    def migrate_url(self) -> str:
        """The URL the migrations run with, as the tables' owner."""
        if not self.migrate_database_url:
            raise ConfigError(f"{MIGRATE_URL_VAR} is not set")
        return self.migrate_database_url
