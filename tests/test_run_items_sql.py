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
# Who helpers.VALID_ROW says wrote a row the owner inserts.
VALID_CREATOR = "session:0a1b2c3d"


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


# ---- an assess run's work item (0016) ----


def holds_assess(owner: psycopg.Connection, number: int = 101, expires: str = "1 hour") -> None:
    """The run of helpers.RUN holds the issue's open assess item, its claim still running."""
    insert_stage_row(
        owner,
        "eeeeeeee",
        assignee="assess",
        subject_ref=str(number),
        claimed_by=RUN.identity,
    )
    owner.execute(
        "UPDATE items SET claim_expires = now() + %s::interval WHERE kind = 'assess'", (expires,)
    )


def insert_work_as_run(app_url: str, **overrides: object) -> None:
    with as_tier(app_url, RUN) as conn:
        insert_stage_row(
            conn, "wwwwwwww", **{"created_by": RUN.identity, "created_run": RUN_ID} | overrides
        )


def test_an_assess_run_inserts_its_issue_s_work_item(
    app_url: str, owner: psycopg.Connection
) -> None:
    holds_assess(owner)
    insert_work_as_run(app_url)
    assert kinds(owner) == [("assess", VALID_CREATOR), ("work", RUN.identity)]


@pytest.mark.parametrize(
    "overrides",
    [
        {"subject_ref": "102"},
        {"subject_repo": "example/other"},
        {"subject_type": "pr", "subject_ref": "101"},
        {"claimed_by": RUN.identity},
        {"assignee": "review"},
        {"assignee": "assess", "subject_ref": "102"},
    ],
    ids=["another issue", "another repo", "a pr", "held", "a review item", "an assess item"],
)
def test_an_assess_run_inserts_no_other_stage_item(
    app_url: str, owner: psycopg.Connection, overrides: dict[str, object]
) -> None:
    holds_assess(owner)
    with pytest.raises(errors.InsufficientPrivilege, match="items_insert_no_run_stage_item"):
        insert_work_as_run(app_url, **overrides)


def test_a_run_whose_assess_claim_ended_inserts_no_work_item(
    app_url: str, owner: psycopg.Connection
) -> None:
    holds_assess(owner, expires="-1 minute")
    with pytest.raises(errors.InsufficientPrivilege, match="items_insert_no_run_stage_item"):
        insert_work_as_run(app_url)
    # Another run's claim, and a closed item, give this run nothing either.
    owner.execute("UPDATE items SET claim_expires = now() + interval '1 hour'")
    owner.execute("UPDATE items SET claimed_by = 'run:20260101-assess-09' WHERE kind = 'assess'")
    with pytest.raises(errors.InsufficientPrivilege, match="items_insert_no_run_stage_item"):
        insert_work_as_run(app_url)


def test_a_wait_whose_date_does_not_parse_closes_and_writes_nothing(
    app_url: str, owner: psycopg.Connection
) -> None:
    holds_assess(owner)
    with as_tier(app_url, RUN) as conn:
        closed = conn.execute(
            "UPDATE items SET closed_at = now(), resolution = 'done', result = %s "
            "WHERE kind = 'assess'",
            (Jsonb({"outcome": "wait", "wait": {"not_before": "next week", "note": "x"}}),),
        )
        assert closed.rowcount == 1
    assert owner.execute("SELECT count(*) FROM items WHERE closed_at IS NULL").fetchone() == (0,)
