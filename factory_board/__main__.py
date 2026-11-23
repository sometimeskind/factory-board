"""Entry point: `python -m factory_board migrate`. The server command comes with the tools."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from factory_board.config import ConfigError, Settings
from factory_board.migrate import MigrationError, apply_migrations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory-board")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply the pending migrations and exit")
    parser.parse_args(argv)

    try:
        settings = Settings.from_env()
        applied = apply_migrations(settings.migrate_url(), settings.migrations_dir)
    except (ConfigError, MigrationError) as exc:
        print(f"factory-board: {exc}", file=sys.stderr)
        return 1
    for label in applied:
        print(f"applied {label}")
    if not applied:
        print("nothing to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
