"""Fixtures: a real Postgres, named by FACTORY_BOARD_TEST_DATABASE_URL.

There is no fake and no skip: a test that needs the database fails without one.
Locally `just test` starts a throwaway Postgres in podman; CI runs a postgres service.
Every test starts from an empty `public` schema, so point the variable at a database
that holds nothing else.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

ENV_VAR = "FACTORY_BOARD_TEST_DATABASE_URL"


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
    return url


@pytest.fixture
def database_url(_test_database_url: str) -> str:
    """The test database's URL, with its public schema emptied."""
    with psycopg.connect(_test_database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return _test_database_url


@pytest.fixture
def conn(database_url: str) -> Iterator[psycopg.Connection]:
    """An autocommit connection to the emptied test database."""
    with psycopg.connect(database_url, autocommit=True) as connection:
        yield connection
