"""No run inserts a stage's item, with plain SQL past the store (0012).

Every repo, number and run here is invented.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from helpers import OPERATOR, RECONCILER, REPO, RUN, SESSION, as_tier, insert_row, insert_stage_row
from psycopg import errors
from psycopg.types.json import Jsonb

from factory_board.kinds import STAGE_KINDS
from factory_board.store import Caller

# The run ID of helpers.RUN, which created_run must repeat.
RUN_ID = RUN.identity.partition(":")[2]
CHECK = "dddddddd"


@pytest.fixture
def owner(migrated: None, owner_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(owner_url, autocommit=True) as connection:
        yield connection


def insert_stage_as(
    app_url: str, caller: Caller, stage_name: str, held_by: str | None = None
) -> None:
    """The caller inserts an open item of the stage, as itself."""
    tier_name, _, run = caller.identity.partition(":")
    with as_tier(app_url, caller) as conn:
        insert_stage_row(
            conn,
            "aaaaaaaa",
            assignee=stage_name,
            claimed_by=held_by,
            created_by=caller.identity,
            created_run=None if tier_name == "process" else run,
        )


def kinds(owner: psycopg.Connection) -> list[tuple[str, str]]:
    return owner.execute("SELECT kind, created_by FROM items ORDER BY created_at, id").fetchall()


@pytest.mark.parametrize("stage_name", STAGE_KINDS)
def test_a_run_inserts_no_stage_item(
    app_url: str, owner: psycopg.Connection, stage_name: str
) -> None:
    for held_by in (None, RUN.identity):
        with pytest.raises(errors.InsufficientPrivilege, match="items_insert_no_run_stage_item"):
            insert_stage_as(app_url, RUN, stage_name, held_by)
    assert kinds(owner) == []


@pytest.mark.parametrize("caller", [SESSION, RECONCILER], ids=lambda c: c.identity)
@pytest.mark.parametrize("stage_name", STAGE_KINDS)
def test_a_session_and_the_reconciler_still_insert_one(
    app_url: str, owner: psycopg.Connection, caller: Caller, stage_name: str
) -> None:
    insert_stage_as(app_url, caller, stage_name)
    assert kinds(owner) == [(stage_name, caller.identity)]


def test_a_run_still_inserts_an_item_for_the_operator(
    app_url: str, owner: psycopg.Connection
) -> None:
    """What a deploy-check run writes for a change that is stuck."""
    with as_tier(app_url, RUN) as conn:
        insert_row(
            conn,
            id="bbbbbbbb",
            kind="decision",
            fields=Jsonb({"kind": "decision", "options": ["wait", "revert"]}),
            claimed_by=OPERATOR.identity,
            created_by=RUN.identity,
            created_run=RUN_ID,
        )
    assert kinds(owner) == [("decision", RUN.identity)]


def test_a_run_s_close_still_wakes_assess(app_url: str, owner: psycopg.Connection) -> None:
    """The board's own assess item, written by its trigger during a run's close."""
    insert_stage_row(
        owner,
        CHECK,
        assignee="deploy-check",
        subject_type="pr",
        subject_ref="202",
        fields=Jsonb(
            {
                "kind": "deploy-check",
                "repo": REPO,
                "number": 202,
                "merge_sha": "c" * 40,
                "issues": [{"repo": REPO, "number": 101}],
            }
        ),
        claimed_by=RUN.identity,
    )
    with as_tier(app_url, RUN) as conn:
        closed = conn.execute(
            "UPDATE items SET closed_at = now(), resolution = 'done', result = %s WHERE id = %s",
            (Jsonb({"outcome": "healthy"}), CHECK),
        )
        assert closed.rowcount == 1
    assert owner.execute(
        "SELECT subject_ref, created_by, claimed_by FROM items "
        "WHERE kind = 'assess' AND closed_at IS NULL"
    ).fetchall() == [("101", "process:board", None)]
