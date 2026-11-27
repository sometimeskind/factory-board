"""The migration runner, against a real Postgres. Table names and SQL here are made up."""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg
import pytest
from helpers import DISPATCHER, OPERATOR, as_tier, free_seat, insert_stage_row

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


def test_shipped_migrations_apply_and_reapply(owner_url: str) -> None:
    """The repo's own migrations/ directory, as the image runs it: as board_owner."""
    first = apply_migrations(owner_url, DEFAULT_MIGRATIONS_DIR)
    assert first == [m.label for m in discover(DEFAULT_MIGRATIONS_DIR)]
    assert first[0] == "0001_items"
    assert apply_migrations(owner_url, DEFAULT_MIGRATIONS_DIR) == []


def test_shipped_migrations_name_a_missing_role(owner_url: str, conn: psycopg.Connection) -> None:
    """The migrations never create a role, and say which one they miss."""
    conn.execute("ALTER ROLE board_process RENAME TO board_process_away")
    try:
        with pytest.raises(MigrationError) as refused:
            apply_migrations(owner_url, DEFAULT_MIGRATIONS_DIR)
    finally:
        conn.execute("ALTER ROLE board_process_away RENAME TO board_process")
    message = str(refused.value)
    assert "0001_items.sql failed" in message
    assert "role board_process does not exist" in message
    assert "dev/roles.sql" in message
    assert not table_exists(conn, "items")
    assert applied(conn) == []


def test_0014_applies_on_a_database_at_0013(
    owner_url: str, app_url: str, tmp_path: Path, conn: psycopg.Connection
) -> None:
    """A board with claims behind it, a counted expiry among them, takes 0014: that expiry
    and a refused result after it write the decision."""
    for path in sorted(DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) <= 13:
            write(tmp_path, path.name, path.read_text())
    assert apply_migrations(owner_url, tmp_path)[-1] == "0013_urgent_assess"
    insert_stage_row(conn, "aaaaaaaa", assignee="review", subject_type="pr", subject_ref="202")

    def claim(run: str) -> None:
        with as_tier(app_url, DISPATCHER) as tier:
            tier.execute(
                "SELECT claim_item(%s, %s, 30, %s, NULL, %s, 'normal')",
                ("aaaaaaaa", run, hashlib.sha256(run.encode()).hexdigest(), free_seat(tier)),
            )

    claim("20260101-review-01")
    conn.execute("UPDATE items SET claim_expires = now() - interval '1 minute'")
    conn.execute("UPDATE claims SET expires_at = now() - interval '1 minute'")
    with as_tier(app_url, DISPATCHER) as tier:
        tier.execute("SELECT * FROM expire_claims(%s)", (OPERATOR.identity,))
    claim("20260101-review-02")
    # At 0013 the release takes three arguments, and a refused result counts nowhere.
    with as_tier(app_url, DISPATCHER) as tier:
        tier.execute("SELECT release_claim('aaaaaaaa', 'stopped', NULL)")
    claim("20260101-review-03")

    assert apply_migrations(owner_url, DEFAULT_MIGRATIONS_DIR) == [
        "0014_result_refused",
        "0015_urgent_failed_run_decision",
    ]
    with as_tier(app_url, DISPATCHER) as tier:
        tier.execute(
            "SELECT release_claim('aaaaaaaa', 'result-refused', NULL, %s)", (OPERATOR.identity,)
        )
    rows = conn.execute(
        "SELECT run, ending, reason, decision_id FROM claims ORDER BY claimed_at, run"
    ).fetchall()
    decision = rows[0][3]
    assert decision is not None
    assert rows == [
        ("20260101-review-01", "expired", None, decision),
        ("20260101-review-02", "released", "stopped", None),
        ("20260101-review-03", "released", "result-refused", decision),
    ]
    assert conn.execute("SELECT * FROM item_after").fetchall() == [("aaaaaaaa", decision)]
    # 0015: the decision is urgent though the item is not.
    assert conn.execute(
        "SELECT i.urgent FROM items AS i ORDER BY i.id = %s", (decision,)
    ).fetchall() == [(False,), (True,)]
