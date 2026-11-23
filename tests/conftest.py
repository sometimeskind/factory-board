"""Fixtures: a real Postgres, named by FACTORY_BOARD_TEST_DATABASE_URL.

There is no fake and no skip: a test that needs the database fails without one.
Locally `just test` starts a throwaway Postgres in podman; CI runs a postgres service.

The variable names a superuser on a database that holds nothing else. The fixtures use it
to create the board's roles (dev/roles.sql) and to empty the `public` schema before every
test. The code under test never connects with it: migrations run as board_owner and the
store as board_app, the way they do in a deployment.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from factory_board.config import DEFAULT_MIGRATIONS_DIR
from factory_board.migrate import apply_migrations
from factory_board.store import Pool, Store, make_pool

ENV_VAR = "FACTORY_BOARD_TEST_DATABASE_URL"
ROLES_SQL = Path(__file__).resolve().parent.parent / "dev" / "roles.sql"


def apply_roles(superuser_url: str) -> None:
    """Create the five roles and give the database to board_owner. Safe to repeat."""
    with psycopg.connect(superuser_url, autocommit=True) as conn:
        conn.execute(ROLES_SQL.read_bytes())


@pytest.fixture(scope="session")
def _test_database_url() -> str:  # pyright: ignore[reportUnusedFunction]
    url = os.environ.get(ENV_VAR, "")
    if not url:
        pytest.fail(
            f"{ENV_VAR} is not set. These tests need a real Postgres: run `just test`, "
            "or start one and export its URL.",
            pytrace=False,
        )
    try:
        psycopg.connect(url, connect_timeout=5).close()
    except psycopg.OperationalError as exc:
        pytest.fail(f"cannot reach the Postgres at {ENV_VAR}: {exc}", pytrace=False)
    apply_roles(url)
    return url


@pytest.fixture
def database_url(_test_database_url: str) -> str:
    """The superuser's URL, with the public schema as a new database has it."""
    with psycopg.connect(_test_database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public AUTHORIZATION pg_database_owner")
        conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
    return _test_database_url


@pytest.fixture
def conn(database_url: str) -> Iterator[psycopg.Connection]:
    """An autocommit superuser connection to the emptied test database."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        yield connection


@pytest.fixture
def owner_url(database_url: str) -> str:
    """The URL the migrations run with: board_owner, which owns the tables."""
    return make_conninfo(database_url, user="board_owner", password="board_owner")


@pytest.fixture
def app_url(database_url: str) -> str:
    """The URL the store connects with: board_app, which holds no rights of its own."""
    return make_conninfo(database_url, user="board_app", password="board_app")


@pytest.fixture
def migrated(owner_url: str) -> None:
    """The shipped migrations, applied as board_owner."""
    apply_migrations(owner_url, DEFAULT_MIGRATIONS_DIR)


@pytest.fixture
async def pool(migrated: None, app_url: str) -> AsyncIterator[Pool]:
    """A pool of one connection, so two calls in a row share it."""
    async with make_pool(app_url, min_size=1, max_size=1) as opened:
        yield opened


@pytest.fixture
def store(pool: Pool) -> Store:
    return Store(pool)
