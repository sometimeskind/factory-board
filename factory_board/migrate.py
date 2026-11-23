"""Applies migrations/NNNN_<name>.sql in order and records them in schema_migrations.

The rules:

- Files are numbered from 0001 with no gap and no duplicate.
- Each file runs in a transaction of its own, together with its schema_migrations row,
  so a failing file leaves no trace and the ones before it stay applied.
- A file that was applied is never edited, renamed or removed: the runner compares the
  SHA-256 of every applied file with the recorded one and refuses to go on if they differ.
- Nothing is applied when a check fails.

The schema_migrations table is the runner's own and is created here, not by a migration:
the runner has to read it before it can decide what to apply.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

# pg_advisory_lock key, so two pods starting together don't apply the same file twice.
_LOCK_KEY = 3001_0001

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    """The migration files and the database disagree, or the files break the rules."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: bytes

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def discover(directory: Path) -> list[Migration]:
    """Read the migration files, in order. Refuses a bad name, a duplicate or a gap."""
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name}: a migration is named NNNN_<name>.sql (lower case, digits, _)"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"{path.name}: version {version:04d} is already used by {found[version].label}.sql"
            )
        sql = path.read_bytes()
        found[version] = Migration(version, match.group(2), hashlib.sha256(sql).hexdigest(), sql)
    migrations = [found[v] for v in sorted(found)]
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"gap in the migrations: expected {expected:04d}, found {migration.label}.sql"
            )
    return migrations


def _pending(migrations: list[Migration], applied: list[tuple[int, str, str]]) -> list[Migration]:
    """Check the applied rows against the files and return the files still to apply."""
    by_version = {m.version: m for m in migrations}
    for position, (version, name, checksum) in enumerate(applied, start=1):
        if version != position:
            raise MigrationError(
                f"schema_migrations has a gap: expected {position:04d}, found {version:04d}"
            )
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"{version:04d}_{name} is applied but its file is missing "
                "(is this image older than the database?)"
            )
        if migration.name != name:
            raise MigrationError(
                f"{version:04d} was applied as {name!r} but the file is now {migration.label}.sql"
            )
        if migration.checksum != checksum:
            raise MigrationError(
                f"{migration.label}.sql changed after it was applied; "
                "add a new migration instead of editing an applied one"
            )
    return migrations[len(applied) :]


def apply_migrations(database_url: str, directory: Path) -> list[str]:
    """Apply every pending migration. Returns the labels applied, in order."""
    migrations = discover(directory)
    done: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            conn.execute(_CREATE_TABLE)
            rows = conn.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            for migration in _pending(migrations, rows):
                try:
                    with conn.transaction():
                        # No parameters, so psycopg sends the file as one simple query
                        # and it may hold several statements.
                        conn.execute(migration.sql)
                        conn.execute(
                            "INSERT INTO schema_migrations (version, name, checksum) "
                            "VALUES (%s, %s, %s)",
                            (migration.version, migration.name, migration.checksum),
                        )
                except psycopg.Error as exc:
                    raise MigrationError(f"{migration.label}.sql failed: {exc}") from exc
                done.append(migration.label)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return done
