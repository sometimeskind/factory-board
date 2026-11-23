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


@dataclass(frozen=True)
class Settings:
    database_url: str
    migrations_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        database_url = env.get("FACTORY_BOARD_DATABASE_URL", "")
        if not database_url:
            raise ConfigError("FACTORY_BOARD_DATABASE_URL is not set")
        migrations_dir = env.get("FACTORY_BOARD_MIGRATIONS_DIR", "")
        return cls(
            database_url=database_url,
            migrations_dir=Path(migrations_dir) if migrations_dir else DEFAULT_MIGRATIONS_DIR,
        )
