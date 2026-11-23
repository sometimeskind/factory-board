"""The migration runner, against a real Postgres. Table names and SQL here are made up."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from factory_board.config import DEFAULT_MIGRATIONS_DIR
from factory_board.migrate import MigrationError, apply_migrations, discover


def write(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql)
    return path


def applied(conn: psycopg.Connection) -> list[tuple[int, str]]:
    return conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()


def table_exists(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (f"public.{name}",)).fetchone()
    assert row is not None
    return row[0] is not None


@pytest.fixture
def two(tmp_path: Path) -> Path:
    """Two migrations; the second only works after the first."""
    write(tmp_path, "0001_widgets.sql", "CREATE TABLE widgets (id integer PRIMARY KEY);")
    write(
        tmp_path,
        "0002_widget_colour.sql",
        "ALTER TABLE widgets ADD COLUMN colour text; INSERT INTO widgets VALUES (1, 'red');",
    )
    return tmp_path


def test_applies_in_order(database_url: str, conn: psycopg.Connection, two: Path) -> None:
    assert apply_migrations(database_url, two) == ["0001_widgets", "0002_widget_colour"]
    assert applied(conn) == [(1, "widgets"), (2, "widget_colour")]
    assert conn.execute("SELECT id, colour FROM widgets").fetchall() == [(1, "red")]


def test_second_run_applies_nothing(database_url: str, conn: psycopg.Connection, two: Path) -> None:
    apply_migrations(database_url, two)
    assert apply_migrations(database_url, two) == []
    assert applied(conn) == [(1, "widgets"), (2, "widget_colour")]


def test_later_run_applies_only_the_new_file(
    database_url: str, conn: psycopg.Connection, two: Path
) -> None:
    apply_migrations(database_url, two)
    write(two, "0003_gadgets.sql", "CREATE TABLE gadgets (id integer);")
    assert apply_migrations(database_url, two) == ["0003_gadgets"]
    assert [version for version, _ in applied(conn)] == [1, 2, 3]


def test_empty_directory_creates_only_the_runner_table(
    database_url: str, conn: psycopg.Connection, tmp_path: Path
) -> None:
    assert apply_migrations(database_url, tmp_path) == []
    assert applied(conn) == []


def test_gap_is_refused_and_nothing_is_applied(
    database_url: str, conn: psycopg.Connection, tmp_path: Path
) -> None:
    write(tmp_path, "0001_widgets.sql", "CREATE TABLE widgets (id integer);")
    write(tmp_path, "0003_gadgets.sql", "CREATE TABLE gadgets (id integer);")
    with pytest.raises(MigrationError, match="gap in the migrations: expected 0002"):
        apply_migrations(database_url, tmp_path)
    assert not table_exists(conn, "widgets")
    assert not table_exists(conn, "schema_migrations")


def test_numbering_must_start_at_one(tmp_path: Path) -> None:
    write(tmp_path, "0002_widgets.sql", "SELECT 1;")
    with pytest.raises(MigrationError, match="expected 0001"):
        discover(tmp_path)


def test_changed_applied_file_is_refused(
    database_url: str, conn: psycopg.Connection, two: Path
) -> None:
    apply_migrations(database_url, two)
    write(two, "0001_widgets.sql", "CREATE TABLE widgets (id bigint PRIMARY KEY);")
    write(two, "0003_gadgets.sql", "CREATE TABLE gadgets (id integer);")
    with pytest.raises(MigrationError, match=r"0001_widgets\.sql changed after it was applied"):
        apply_migrations(database_url, two)
    assert not table_exists(conn, "gadgets")
    assert [version for version, _ in applied(conn)] == [1, 2]


def test_renamed_applied_file_is_refused(database_url: str, two: Path) -> None:
    apply_migrations(database_url, two)
    (two / "0002_widget_colour.sql").rename(two / "0002_widget_color.sql")
    with pytest.raises(MigrationError, match="was applied as 'widget_colour'"):
        apply_migrations(database_url, two)


def test_missing_applied_file_is_refused(database_url: str, two: Path) -> None:
    apply_migrations(database_url, two)
    (two / "0002_widget_colour.sql").unlink()
    with pytest.raises(MigrationError, match="0002_widget_colour is applied but its file"):
        apply_migrations(database_url, two)


def test_failing_file_rolls_back_alone(
    database_url: str, conn: psycopg.Connection, tmp_path: Path
) -> None:
    write(tmp_path, "0001_widgets.sql", "CREATE TABLE widgets (id integer);")
    write(
        tmp_path,
        "0002_broken.sql",
        "CREATE TABLE gadgets (id integer); INSERT INTO no_such_table VALUES (1);",
    )
    write(tmp_path, "0003_never.sql", "CREATE TABLE never (id integer);")
    with pytest.raises(MigrationError, match=r"0002_broken\.sql failed"):
        apply_migrations(database_url, tmp_path)
    assert applied(conn) == [(1, "widgets")]
    assert table_exists(conn, "widgets")
    assert not table_exists(conn, "gadgets")
    assert not table_exists(conn, "never")

    # The fixed file applies on the next run: 0002 was never recorded.
    write(tmp_path, "0002_broken.sql", "CREATE TABLE gadgets (id integer);")
    assert apply_migrations(database_url, tmp_path) == ["0002_broken", "0003_never"]


@pytest.mark.parametrize("name", ["1_widgets.sql", "0001-widgets.sql", "0001_Widgets.sql", "x.sql"])
def test_badly_named_file_is_refused(tmp_path: Path, name: str) -> None:
    write(tmp_path, name, "SELECT 1;")
    with pytest.raises(MigrationError, match=r"is named NNNN_<name>\.sql"):
        discover(tmp_path)


def test_duplicate_version_is_refused(tmp_path: Path) -> None:
    write(tmp_path, "0001_gadgets.sql", "SELECT 1;")
    write(tmp_path, "0001_widgets.sql", "SELECT 1;")
    with pytest.raises(MigrationError, match="version 0001 is already used by 0001_gadgets"):
        discover(tmp_path)


def test_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="migrations directory not found"):
        discover(tmp_path / "nope")


def test_files_that_are_not_sql_are_ignored(tmp_path: Path) -> None:
    write(tmp_path, "README.md", "notes")
    write(tmp_path, "0001_widgets.sql", "SELECT 1;")
    assert [m.label for m in discover(tmp_path)] == ["0001_widgets"]


def test_shipped_migrations_apply_and_reapply(database_url: str) -> None:
    """The repo's own migrations/ directory, as the image runs it."""
    first = apply_migrations(database_url, DEFAULT_MIGRATIONS_DIR)
    assert first == [m.label for m in discover(DEFAULT_MIGRATIONS_DIR)]
    assert apply_migrations(database_url, DEFAULT_MIGRATIONS_DIR) == []
