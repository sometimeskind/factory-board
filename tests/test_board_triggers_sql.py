"""What the board does by itself, with plain SQL past the store: it wakes assess, counts
the operator's acts, refuses a fourth fix-forward item and keeps the reconciler's heartbeat.

Every repo, number, run and hash here is invented.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator
from typing import Any, LiteralString

import psycopg
import pytest
from helpers import (
    DISPATCHER,
    NIGHTLY,
    OPERATOR,
    RECONCILER,
    REPO,
    RUN,
    SESSION,
    as_tier,
    insert_row,
    insert_stage_row,
)
from psycopg import errors, sql
from psycopg.types.json import Jsonb

from factory_board.kinds import Assess, parse_stage_item
from factory_board.store import Caller

EVERY_TIER = [OPERATOR, SESSION, RECONCILER]
CLOSE_AS = "UPDATE items SET closed_at = now(), resolution = %s, result = %s WHERE id = %s"
BEAT = "SELECT record_heartbeat()"
OPERATORS = "operator-items-closed"
DEPLOYS = "deploy-checks-closed"
FIRST = "aaaaaaaa"
SECOND = "bbbbbbbb"
THIRD = "cccccccc"
CHECK = "dddddddd"


@pytest.fixture
def owner(migrated: None, owner_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(owner_url, autocommit=True) as connection:
        yield connection


def close(
    app_url: str,
    item_id: str,
    resolution: str = "done",
    *,
    by: Caller = OPERATOR,
    result: dict[str, Any] | None = None,
) -> None:
    with as_tier(app_url, by) as conn:
        done = conn.execute(
            CLOSE_AS, (resolution, None if result is None else Jsonb(result), item_id)
        )
        assert done.rowcount == 1


def assessments(owner: psycopg.Connection, number: int = 101) -> list[tuple[Any, ...]]:
    """The open assess items of an issue: (reasons, not_before, holder, creator, urgent)."""
    return owner.execute(
        "SELECT reasons, not_before, claimed_by, created_by, urgent FROM items "
        "WHERE kind = 'assess' AND closed_at IS NULL AND subject_type = 'issue' "
        "AND subject_repo = %s AND subject_ref = %s ORDER BY created_at, id",
        (REPO, str(number)),
    ).fetchall()


def woken(owner: psycopg.Connection, number: int = 101) -> list[list[str]]:
    """The reasons of each open assess item of an issue."""
    return [row[0] for row in assessments(owner, number)]


def acted(owner: psycopg.Connection) -> list[str]:
    """The issues the operator has acted on."""
    rows = owner.execute(
        "SELECT subject_ref FROM operator_acts WHERE subject_repo = %s ORDER BY subject_ref::int",
        (REPO,),
    ).fetchall()
    return [row[0] for row in rows]


def check_fields(*numbers: int) -> Jsonb:
    """A deploy-check's fields, naming these made-up issues."""
    return Jsonb(
        {
            "kind": "deploy-check",
            "repo": REPO,
            "number": 202,
            "merge_sha": "c" * 40,
            "issues": [{"repo": REPO, "number": number} for number in numbers],
        }
    )


def insert_check(
    owner: psycopg.Connection, item_id: str, *numbers: int, pr: int = 202, **overrides: Any
) -> None:
    insert_stage_row(
        owner,
        item_id,
        assignee="deploy-check",
        subject_type="pr",
        subject_ref=str(pr),
        fields=check_fields(*numbers),
        **overrides,
    )


def insert_pr_item(owner: psycopg.Connection, item_id: str, **overrides: Any) -> None:
    """A pr-approve item on a made-up PR, whose ticket is issue 101."""
    insert_row(
        owner,
        **{
            "id": item_id,
            "kind": "pr-approve",
            "subject_type": "pr",
            "subject_ref": "202",
            "fields": Jsonb({"kind": "pr-approve", "ticket": {"repo": REPO, "number": 101}}),
        }
        | overrides,
    )


# ---- The operator's last open item on an issue closed ----


def test_the_last_item_s_close_writes_the_issue_s_assess_item(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_row(owner, id=FIRST)
    assert assessments(owner) == []
    close(app_url, FIRST)
    assert assessments(owner) == [([OPERATORS], None, None, "process:board", False)]
    row = owner.execute("SELECT fields FROM items WHERE kind = 'assess'").fetchone()
    assert row is not None
    parsed = parse_stage_item(row[0])
    assert isinstance(parsed, Assess)
    assert (parsed.issue.repo, parsed.issue.number, parsed.note) == (REPO, 101, None)


@pytest.mark.parametrize("resolution", ["done", "answered", "replied", "rejected", "superseded"])
def test_it_does_not_matter_how_the_last_item_closed(
    app_url: str, owner: psycopg.Connection, resolution: str
) -> None:
    insert_row(owner, id=FIRST)
    close(app_url, FIRST, resolution)
    assert woken(owner) == [[OPERATORS]]


def test_an_item_closed_by_the_process_that_holds_it_wakes_too(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_row(owner, id=FIRST, claimed_by=NIGHTLY.identity)
    close(app_url, FIRST, "superseded", by=NIGHTLY)
    assert woken(owner) == [[OPERATORS]]


@pytest.mark.parametrize(
    "other",
    [{}, {"not_before": "2999-01-01T00:00:00+00:00"}, {"claimed_by": NIGHTLY.identity}],
    ids=["open", "deferred", "held-by-a-process"],
)
def test_nothing_is_woken_while_another_item_holds_the_issue(
    app_url: str, owner: psycopg.Connection, other: dict[str, Any]
) -> None:
    insert_row(owner, id=FIRST)
    insert_row(owner, id=SECOND, **other)
    close(app_url, FIRST)
    assert woken(owner) == []
    close(app_url, SECOND, "rejected")
    assert woken(owner) == [[OPERATORS]]


def test_a_waiting_item_is_open_too(app_url: str, owner: psycopg.Connection) -> None:
    insert_row(owner, id=FIRST, subject_ref="102")
    insert_row(owner, id=SECOND)
    insert_row(owner, id=THIRD)
    owner.execute("INSERT INTO item_after (item_id, after_id) VALUES (%s, %s)", (THIRD, FIRST))
    close(app_url, SECOND)
    assert woken(owner) == []
    # Done lets the waiting item go, and it stays open: issue 101 still waits.
    close(app_url, FIRST)
    assert (woken(owner), woken(owner, 102)) == ([], [[OPERATORS]])


def test_an_item_that_does_not_hold_wakes_nothing_and_holds_nothing(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_row(owner, id=FIRST, holds=False)
    insert_row(owner, id=SECOND)
    close(app_url, SECOND)
    assert woken(owner) == [[OPERATORS]]
    owner.execute(
        "UPDATE items SET closed_at = now(), resolution = 'superseded' WHERE kind = 'assess'"
    )

    insert_row(owner, id=THIRD, holds=False)
    close(app_url, THIRD, "rejected", result={"reason": "not needed"})
    close(app_url, FIRST, "replied")
    assert woken(owner) == []


def test_only_the_item_s_own_issue_is_woken(app_url: str, owner: psycopg.Connection) -> None:
    insert_row(owner, id=FIRST)
    insert_row(owner, id=SECOND, subject_ref="102")
    insert_row(owner, id=THIRD, subject_repo="example/other")
    close(app_url, FIRST)
    assert (woken(owner), woken(owner, 102)) == ([[OPERATORS]], [])
    assert owner.execute("SELECT count(*) FROM items WHERE kind = 'assess'").fetchone() == (1,)


def test_an_item_about_no_issue_wakes_nothing(app_url: str, owner: psycopg.Connection) -> None:
    insert_row(
        owner,
        id=FIRST,
        subject_repo=None,
        subject_type="finding",
        subject_ref="finding:widget-press-hot",
        key="finding:widget-press-hot",
    )
    close(app_url, FIRST, "rejected")
    assert owner.execute("SELECT count(*) FROM items WHERE kind = 'assess'").fetchone() == (0,)


def test_a_reject_that_closes_a_chain_wakes_each_issue_once(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_row(owner, id=FIRST)
    insert_row(owner, id=SECOND)
    insert_row(owner, id=THIRD, subject_ref="102")
    for waiting in (SECOND, THIRD):
        owner.execute(
            "INSERT INTO item_after (item_id, after_id) VALUES (%s, %s)", (waiting, FIRST)
        )
    close(app_url, FIRST, "rejected", result={"reason": "the press is sold"})
    assert (woken(owner), woken(owner, 102)) == ([[OPERATORS]], [[OPERATORS]])


def test_a_wake_merges_into_the_open_assess_item(app_url: str, owner: psycopg.Connection) -> None:
    insert_stage_row(
        owner,
        SECOND,
        assignee="assess",
        not_before="2999-01-01T00:00:00+00:00",
        reasons=["dated"],
        created_by=RECONCILER.identity,
        urgent=True,
    )
    insert_row(owner, id=FIRST)
    close(app_url, FIRST)
    # The date is gone (no date is the earliest), the reason is appended, the rest stays.
    assert assessments(owner) == [(["dated", OPERATORS], None, None, RECONCILER.identity, True)]
    insert_row(owner, id=THIRD)
    close(app_url, THIRD)
    assert woken(owner) == [["dated", OPERATORS]]


def test_two_closes_at_once_wake_the_issue_once(app_url: str, owner: psycopg.Connection) -> None:
    """Without the lock each close would see the other's item still open, and neither
    would wake anything."""
    insert_row(owner, id=FIRST)
    insert_row(owner, id=SECOND)
    finished = threading.Event()

    def second() -> None:
        close(app_url, SECOND)
        finished.set()

    with as_tier(app_url, OPERATOR) as first:
        first.execute(CLOSE_AS, ("done", None, FIRST))
        other = threading.Thread(target=second)
        other.start()
        time.sleep(0.5)
        # The second close waits for the first transaction to end.
        assert not finished.is_set()
    other.join(timeout=10)
    assert finished.is_set()
    assert woken(owner) == [[OPERATORS]]


# ---- A PR item says no ----


@pytest.mark.parametrize("resolution", ["rejected", "replied"])
def test_a_refused_pr_item_wakes_its_ticket_s_issue(
    app_url: str, owner: psycopg.Connection, resolution: str
) -> None:
    insert_pr_item(owner, FIRST)
    close(app_url, FIRST, resolution)
    assert woken(owner) == [[OPERATORS]]


@pytest.mark.parametrize("resolution", ["done", "superseded"])
def test_a_pr_item_that_went_through_or_was_replaced_wakes_nothing(
    app_url: str, owner: psycopg.Connection, resolution: str
) -> None:
    insert_pr_item(owner, FIRST, claimed_by=RECONCILER.identity)
    close(app_url, FIRST, resolution, by=RECONCILER, result={"cause": "approved"})
    assert woken(owner) == []


def test_a_refused_pr_item_wakes_nothing_while_an_item_holds_the_issue(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_pr_item(owner, FIRST)
    insert_row(owner, id=SECOND)
    close(app_url, FIRST, "rejected")
    assert woken(owner) == []
    # And an open PR item holds no issue: the other item's close wakes.
    insert_pr_item(owner, THIRD)
    close(app_url, SECOND)
    assert woken(owner) == [[OPERATORS]]


def test_a_refused_pr_item_with_no_ticket_wakes_nothing(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_pr_item(owner, FIRST, fields=Jsonb({"kind": "pr-approve", "ticket": None}))
    close(app_url, FIRST, "rejected")
    assert owner.execute("SELECT count(*) FROM items WHERE kind = 'assess'").fetchone() == (0,)


# ---- The decision after two expired claims ----


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def stuck(owner: psycopg.Connection, app_url: str, kind: str) -> str:
    """A stage's item on issue 101 whose claim ran out twice; the decision item's ID."""
    insert_stage_row(owner, FIRST, assignee=kind)
    decision = None
    for run in (f"20260101-{kind}-01", f"20260101-{kind}-02"):
        with as_tier(app_url, DISPATCHER) as conn:
            conn.execute(
                "SELECT claim_item(%s, %s, %s, %s, %s)",
                (FIRST, run, 30, sha(run), "hash-1" if kind == "assess" else None),
            )
        owner.execute(
            "UPDATE items SET claim_expires = now() - interval '1 minute' WHERE id = %s", (FIRST,)
        )
        owner.execute(
            "UPDATE claims SET expires_at = now() - interval '1 minute' WHERE ended_at IS NULL"
        )
        with as_tier(app_url, DISPATCHER) as conn:
            rows = conn.execute("SELECT * FROM expire_claims(%s)", (OPERATOR.identity,)).fetchall()
        decision = rows[0][2]
    assert decision is not None
    return decision


def is_open(owner: psycopg.Connection, item_id: str) -> bool:
    row = owner.execute("SELECT closed_at IS NULL FROM items WHERE id = %s", (item_id,)).fetchone()
    assert row is not None
    return row[0]


def test_try_again_wakes_nothing(app_url: str, owner: psycopg.Connection) -> None:
    decision = stuck(owner, app_url, "work")
    close(app_url, decision, "answered", result={"option": 1, "answer": "Try again"})
    assert is_open(owner, FIRST)
    assert woken(owner) == []


@pytest.mark.parametrize(
    ("resolution", "result"),
    [("answered", {"option": 2, "answer": "Stop"}), ("rejected", {"reason": "no"})],
    ids=["stop", "reject"],
)
def test_a_stopped_work_item_s_issue_goes_back_to_assess(
    app_url: str, owner: psycopg.Connection, resolution: str, result: dict[str, Any]
) -> None:
    decision = stuck(owner, app_url, "work")
    close(app_url, decision, resolution, result=result)
    assert not is_open(owner, FIRST)
    assert woken(owner) == [[OPERATORS]]


@pytest.mark.parametrize(
    ("resolution", "result"),
    [
        ("answered", {"option": 1, "answer": "Try again"}),
        ("answered", {"option": 2, "answer": "Stop"}),
        ("rejected", {"reason": "no"}),
    ],
    ids=["try-again", "stop", "reject"],
)
def test_a_stuck_assess_item_s_decision_never_writes_another(
    app_url: str, owner: psycopg.Connection, resolution: str, result: dict[str, Any]
) -> None:
    decision = stuck(owner, app_url, "assess")
    close(app_url, decision, resolution, result=result)
    again = result.get("option") == 1
    assert is_open(owner, FIRST) is again
    # The one item it was: no reason added, and no second item once it is stopped.
    assert woken(owner) == ([[]] if again else [])


# ---- A wake that meets an assess item a run holds ----

RUN_ID = RUN.identity.partition(":")[2]
HELD = "hhhhhhhh"


def hold(owner: psycopg.Connection, app_url: str, **overrides: Any) -> None:
    """Issue 101's assess item, written for one reason and claimed for the run of RUN."""
    insert_stage_row(owner, HELD, assignee="assess", reasons=["changed"], **overrides)
    with as_tier(app_url, DISPATCHER) as conn:
        conn.execute(
            "SELECT claim_item(%s, %s, %s, %s, %s)", (HELD, RUN_ID, 30, sha(RUN_ID), "hash-1")
        )


def wake(owner: psycopg.Connection, app_url: str, item_id: str = FIRST) -> None:
    """The operator's last item on issue 101 closes."""
    insert_row(owner, id=item_id)
    close(app_url, item_id)


def reasons_of(owner: psycopg.Connection, item_id: str = HELD) -> tuple[Any, ...]:
    """(reasons, pending_reasons, holder, open) of an item."""
    row = owner.execute(
        "SELECT reasons, pending_reasons, claimed_by, closed_at IS NULL FROM items WHERE id = %s",
        (item_id,),
    ).fetchone()
    assert row is not None
    return row


def test_a_wake_during_a_claim_waits_on_the_held_item(
    app_url: str, owner: psycopg.Connection
) -> None:
    hold(owner, app_url)
    wake(owner, app_url)
    # What the run was started on is not changed, and no second item is written.
    assert reasons_of(owner) == (["changed"], [OPERATORS], RUN.identity, True)
    assert woken(owner) == [["changed"]]


def test_the_close_of_the_held_item_writes_the_next_one_with_what_waited(
    app_url: str, owner: psycopg.Connection
) -> None:
    hold(owner, app_url)
    wake(owner, app_url)
    close(app_url, HELD, by=RUN, result={"outcome": "wait"})
    # The record is cleared, and the next item is the board's: due now and held by nobody.
    assert reasons_of(owner) == (["changed"], [], RUN.identity, False)
    assert assessments(owner) == [([OPERATORS], None, None, "process:board", False)]


def test_two_reasons_that_waited_both_reach_the_next_item(
    app_url: str, owner: psycopg.Connection
) -> None:
    hold(owner, app_url)
    wake(owner, app_url)
    insert_check(owner, CHECK, 101)
    close(app_url, CHECK, "superseded", by=RECONCILER)
    assert reasons_of(owner)[1] == [OPERATORS, DEPLOYS]
    close(app_url, HELD, by=RUN)
    assert woken(owner) == [[OPERATORS, DEPLOYS]]


def test_the_same_wake_twice_waits_once(app_url: str, owner: psycopg.Connection) -> None:
    hold(owner, app_url)
    wake(owner, app_url)
    wake(owner, app_url, SECOND)
    assert reasons_of(owner)[1] == [OPERATORS]
    # A reason the item was written for is still kept when it arrives again during the run.
    owner.execute("UPDATE items SET reasons = %s WHERE id = %s", ([OPERATORS], HELD))
    wake(owner, app_url, THIRD)
    assert reasons_of(owner)[:2] == ([OPERATORS], [OPERATORS])
    close(app_url, HELD, by=RUN)
    assert woken(owner) == [[OPERATORS]]


def test_a_close_with_nothing_pending_writes_nothing(
    app_url: str, owner: psycopg.Connection
) -> None:
    hold(owner, app_url)
    close(app_url, HELD, by=RUN)
    assert woken(owner) == []
    assert owner.execute("SELECT count(*) FROM items").fetchone() == (1,)


@pytest.mark.parametrize("route", ["reject", "cascade"])
def test_the_held_item_s_close_carries_what_waited_however_it_closed(
    app_url: str, owner: psycopg.Connection, route: str
) -> None:
    hold(owner, app_url)
    wake(owner, app_url)
    if route == "reject":
        close(app_url, HELD, "rejected", result={"reason": "stop this"})
    else:
        # The held item waits on an item of another issue, which the operator refuses.
        insert_row(owner, id=SECOND, subject_ref="102")
        owner.execute("INSERT INTO item_after (item_id, after_id) VALUES (%s, %s)", (HELD, SECOND))
        close(app_url, SECOND, "replied")
    assert reasons_of(owner)[1:] == ([], RUN.identity, False)
    assert woken(owner) == [[OPERATORS]]


def test_a_release_hands_what_waited_to_the_item(app_url: str, owner: psycopg.Connection) -> None:
    hold(owner, app_url)
    wake(owner, app_url)
    with as_tier(app_url, RUN) as conn:
        conn.execute("SELECT release_claim(%s, %s, NULL)", (HELD, "usage-limit"))
    assert reasons_of(owner) == (["changed", OPERATORS], [], None, True)
    with as_tier(app_url, DISPATCHER) as conn:
        assert conn.execute("SELECT id FROM claimable").fetchall() == [(HELD,)]
    # The next run holds it with nothing pending, and its close writes nothing more.
    with as_tier(app_url, DISPATCHER) as conn:
        run = "20260101-assess-02"
        conn.execute("SELECT claim_item(%s, %s, %s, %s, %s)", (HELD, run, 30, sha(run), "hash-1"))
    owner.execute("UPDATE items SET closed_at = now(), resolution = 'done' WHERE id = %s", (HELD,))
    assert woken(owner) == []


def test_a_claim_that_runs_out_hands_what_waited_to_the_item(
    app_url: str, owner: psycopg.Connection
) -> None:
    hold(owner, app_url)
    wake(owner, app_url)
    owner.execute("UPDATE items SET claim_expires = now() - interval '1 minute'")
    owner.execute("UPDATE claims SET expires_at = now() - interval '1 minute'")
    with as_tier(app_url, DISPATCHER) as conn:
        conn.execute("SELECT * FROM expire_claims(%s)", (OPERATOR.identity,))
    assert reasons_of(owner) == (["changed", OPERATORS], [], None, True)


def test_a_wake_after_the_item_is_back_in_the_queue_merges_as_before(
    app_url: str, owner: psycopg.Connection
) -> None:
    hold(owner, app_url)
    with as_tier(app_url, DISPATCHER) as conn:
        conn.execute("SELECT release_claim(%s, %s, NULL)", (HELD, "stopped"))
    wake(owner, app_url)
    assert reasons_of(owner) == (["changed", OPERATORS], [], None, True)


def test_a_wake_on_an_item_that_waits_on_its_decision_merges_and_goes_with_it(
    app_url: str, owner: psycopg.Connection
) -> None:
    """After two expired claims no run holds the item, so nothing is pending on it."""
    decision = stuck(owner, app_url, "assess")
    insert_check(owner, CHECK, 101)
    close(app_url, CHECK, "superseded", by=RECONCILER)
    assert reasons_of(owner, FIRST) == ([DEPLOYS], [], None, True)
    close(app_url, decision, "answered", result={"option": 2, "answer": "Stop"})
    assert reasons_of(owner, FIRST)[3] is False
    assert woken(owner) == []


def test_the_reconciler_s_trigger_on_a_held_item_is_still_dropped(
    app_url: str, owner: psycopg.Connection
) -> None:
    """Its facts are on GitHub: the next pass reads them again, and the hash decides."""
    hold(owner, app_url)
    with as_tier(app_url, RECONCILER) as conn:
        merged = conn.execute(
            "SELECT merge_assess_trigger(%s, NULL, ARRAY['reopened'])", (HELD,)
        ).fetchone()
    assert merged == (False,)
    assert reasons_of(owner) == (["changed"], [], RUN.identity, True)


@pytest.mark.parametrize("caller", [*EVERY_TIER, RUN, DISPATCHER], ids=lambda c: c.identity)
def test_no_tier_writes_what_is_pending(
    app_url: str, owner: psycopg.Connection, caller: Caller
) -> None:
    hold(owner, app_url)
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        conn.execute("UPDATE items SET pending_reasons = ARRAY['new'] WHERE id = %s", (HELD,))
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        insert_stage_row(
            conn,
            SECOND,
            assignee="assess",
            subject_ref="102",
            created_by=caller.identity,
            pending_reasons=["new"],
        )
    with as_tier(app_url, caller) as conn:
        assert conn.execute("SELECT pending_reasons FROM items").fetchall() == [([],)]


def test_only_an_assess_item_has_reasons_pending(owner: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="items_pending_reasons_on_assess"):
        insert_stage_row(owner, FIRST, pending_reasons=["new"])


# ---- The last open deploy-check on an issue closed ----


def test_a_deploy_check_s_close_wakes_each_issue_it_names(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_check(owner, CHECK, 101, 102, claimed_by=RUN.identity)
    close(app_url, CHECK, by=RUN, result={"outcome": "healthy"})
    assert (woken(owner), woken(owner, 102)) == ([[DEPLOYS]], [[DEPLOYS]])
    assert assessments(owner)[0][2:] == (None, "process:board", False)


def test_a_deploy_check_wakes_nothing_while_another_names_the_issue(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_check(owner, CHECK, 101, 102)
    insert_check(owner, SECOND, 101, pr=203)
    close(app_url, CHECK, "superseded", by=RECONCILER, result={"cause": "reverted"})
    assert (woken(owner), woken(owner, 102)) == ([], [[DEPLOYS]])
    close(app_url, SECOND, "rejected")
    assert woken(owner) == [[DEPLOYS]]


def test_a_deploy_check_wakes_whatever_the_operator_has_open(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_row(owner, id=FIRST)
    insert_check(owner, CHECK, 101)
    close(app_url, CHECK, "superseded", by=RECONCILER)
    assert woken(owner) == [[DEPLOYS]]
    close(app_url, FIRST)
    assert woken(owner) == [[DEPLOYS, OPERATORS]]


def test_a_deploy_check_written_closed_wakes_too(app_url: str, owner: psycopg.Connection) -> None:
    with as_tier(app_url, RECONCILER) as conn:
        insert_stage_row(
            conn,
            CHECK,
            assignee="deploy-check",
            subject_type="pr",
            subject_ref="202",
            fields=check_fields(101),
            created_by=RECONCILER.identity,
        )
        conn.execute(CLOSE_AS, ("done", Jsonb({"outcome": "nothing-to-deploy"}), CHECK))
    assert woken(owner) == [[DEPLOYS]]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"issues": []},
        {"issues": "101"},
        {"issues": [101, None, "x", {"repo": 5, "number": 101}, {"repo": REPO, "number": "x"}]},
        {"issues": [{"repo": REPO}, {"number": 101}, {"repo": "no-slash", "number": 101}]},
    ],
    ids=["none", "empty", "not-a-list", "not-tickets", "half-tickets"],
)
def test_a_deploy_check_that_names_no_issue_closes_and_wakes_nothing(
    app_url: str, owner: psycopg.Connection, fields: dict[str, Any]
) -> None:
    insert_stage_row(
        owner,
        CHECK,
        assignee="deploy-check",
        subject_type="pr",
        subject_ref="202",
        fields=Jsonb(fields),
    )
    close(app_url, CHECK, "superseded", by=RECONCILER)
    assert owner.execute("SELECT count(*) FROM items WHERE kind = 'assess'").fetchone() == (0,)


@pytest.mark.parametrize("stage_name", ["review", "merge", "work"])
def test_no_other_stage_s_close_wakes(
    app_url: str, owner: psycopg.Connection, stage_name: str
) -> None:
    insert_stage_row(
        owner,
        FIRST,
        assignee=stage_name,
        subject_type="pr",
        subject_ref="202",
        fields=check_fields(101),
    )
    insert_stage_row(owner, SECOND, assignee="work")
    for item_id in (FIRST, SECOND):
        close(app_url, item_id, "rejected")
    assert owner.execute("SELECT count(*) FROM items WHERE kind = 'assess'").fetchone() == (0,)


# ---- The operator acted ----


@pytest.mark.parametrize(
    "verb",
    [
        "UPDATE items SET closed_at = now(), resolution = 'done' WHERE id = %s",
        "UPDATE items SET closed_at = now(), resolution = 'rejected' WHERE id = %s",
        "UPDATE items SET not_before = now() + interval '1 day' WHERE id = %s",
        "UPDATE items SET urgent = true WHERE id = %s",
        "INSERT INTO replies (item_id, author, body) VALUES (%s, 'human:operator', 'no')",
    ],
    ids=["done", "reject", "defer", "bump", "reply"],
)
def test_every_verb_of_the_operator_s_is_an_act_on_the_item_s_issue(
    app_url: str, owner: psycopg.Connection, verb: LiteralString
) -> None:
    insert_row(owner, id=FIRST, claimed_by=NIGHTLY.identity)
    insert_row(owner, id=SECOND, subject_ref="102")
    assert acted(owner) == []
    with as_tier(app_url, OPERATOR) as conn:
        conn.execute(verb, (FIRST,))
    assert acted(owner) == ["101"]


def test_an_act_is_kept_once_per_issue_with_its_last_time(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_row(owner, id=FIRST)
    with as_tier(app_url, OPERATOR) as conn:
        conn.execute("UPDATE items SET urgent = true WHERE id = %s", (FIRST,))
    first = owner.execute("SELECT acted_at FROM operator_acts").fetchall()
    close(app_url, FIRST)
    second = owner.execute("SELECT acted_at FROM operator_acts").fetchall()
    assert len(first) == len(second) == 1
    assert second[0][0] > first[0][0]


def test_an_act_on_a_stage_s_item_and_on_a_pr_item_counts_for_their_issues(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_stage_row(owner, FIRST, subject_ref="103")
    insert_pr_item(owner, SECOND)
    insert_check(owner, CHECK, 104, 105)
    insert_stage_row(owner, THIRD, assignee="review", subject_type="pr", subject_ref="203")
    with as_tier(app_url, OPERATOR) as conn:
        conn.execute("UPDATE items SET urgent = true")
    # The review item is about a PR and names no issue.
    assert acted(owner) == ["101", "103", "104", "105"]


def test_a_reject_s_chain_is_an_act_on_each_item_s_issue(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_row(owner, id=FIRST)
    insert_row(owner, id=SECOND, subject_ref="102")
    owner.execute("INSERT INTO item_after (item_id, after_id) VALUES (%s, %s)", (SECOND, FIRST))
    close(app_url, FIRST, "rejected")
    assert acted(owner) == ["101", "102"]


def test_what_a_process_or_a_session_does_is_no_act(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_row(owner, id=FIRST, claimed_by=NIGHTLY.identity)
    close(app_url, FIRST, "superseded", by=NIGHTLY)
    insert_stage_row(owner, SECOND, assignee="assess", subject_ref="102")
    with as_tier(app_url, RECONCILER) as conn:
        conn.execute("SELECT merge_assess_trigger(%s, NULL, ARRAY['changed'])", (SECOND,))
    with as_tier(app_url, DISPATCHER) as conn:
        conn.execute(
            "SELECT claim_item(%s, %s, %s, %s, %s)",
            (SECOND, "20260101-assess-01", 30, sha("x"), "hash-1"),
        )
    insert_row(
        owner,
        id=THIRD,
        kind="pr-merge",
        subject_type="pr",
        subject_ref="202",
        claimed_by=RECONCILER.identity,
        fields=Jsonb({"ticket": {"repo": REPO, "number": 103}}),
    )
    close(app_url, THIRD, by=RECONCILER, result={"cause": "merged"})
    insert_stage_row(owner, CHECK, subject_ref="104", claimed_by=SESSION.identity)
    close(app_url, CHECK, by=SESSION)
    assert acted(owner) == []


def test_an_approval_the_reconciler_sees_is_the_operator_s_act(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_pr_item(owner, FIRST, claimed_by=RECONCILER.identity)
    close(app_url, FIRST, "superseded", by=RECONCILER, result={"cause": "head-moved"})
    assert acted(owner) == []
    insert_pr_item(owner, SECOND, claimed_by=RECONCILER.identity)
    close(app_url, SECOND, by=RECONCILER, result={"cause": "approved"})
    assert acted(owner) == ["101"]


@pytest.mark.parametrize("caller", EVERY_TIER, ids=lambda c: c.identity)
@pytest.mark.parametrize("table", ["operator_acts", "heartbeats"])
def test_every_tier_reads_the_board_s_own_tables_and_none_writes_them(
    app_url: str, owner: psycopg.Connection, caller: Caller, table: str
) -> None:
    owner.execute(
        "INSERT INTO operator_acts (subject_repo, subject_ref, acted_at) VALUES (%s, '101', now())",
        (REPO,),
    )
    owner.execute("INSERT INTO heartbeats (process, beat_at) VALUES ('reconciler', now())")
    with as_tier(app_url, caller) as conn:
        count = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        assert conn.execute(count).fetchone() == (1,)
    writes: dict[str, list[LiteralString]] = {
        "operator_acts": [
            "INSERT INTO operator_acts (subject_repo, subject_ref, acted_at) "
            "VALUES ('example/repo', '102', now())",
            "UPDATE operator_acts SET acted_at = now()",
            "DELETE FROM operator_acts",
        ],
        "heartbeats": [
            "INSERT INTO heartbeats (process, beat_at) VALUES ('dispatcher', now())",
            "UPDATE heartbeats SET beat_at = now()",
            "DELETE FROM heartbeats",
        ],
    }
    for statement in writes[table]:
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(statement)


# ---- The fix-forward limit ----

WORK = (
    "INSERT INTO items (id, assignee, kind, subject_repo, subject_type, subject_ref, "
    "created_by, created_run) VALUES (%s, 'work', 'work', %s, 'issue', %s, %s, %s) "
    "ON CONFLICT DO NOTHING"
)


def assess_writes(app_url: str, item_id: str, number: int = 101) -> int:
    """A run writes the issue's work item, as an assess run will; the rows written."""
    with as_tier(app_url, RUN) as conn:
        return conn.execute(WORK, (item_id, REPO, str(number), RUN.identity, RUN_ID)).rowcount


def worked(
    owner: psycopg.Connection, item_id: str, number: int = 101, by: str = "run:20260101-assess-09"
) -> None:
    """A closed work item on the issue, written a moment ago."""
    insert_stage_row(
        owner,
        item_id,
        subject_ref=str(number),
        created_by=by,
        closed_at="now",
        resolution="done",
    )


def limit(owner: psycopg.Connection) -> list[tuple[Any, ...]]:
    return owner.execute(
        "SELECT subject_ref, work_items, at_limit FROM fix_forward ORDER BY subject_ref::int"
    ).fetchall()


def test_a_fourth_work_item_from_assess_is_refused(app_url: str, owner: psycopg.Connection) -> None:
    for item_id in ("wwwwwww2", "wwwwwww3"):
        worked(owner, item_id)
    assert limit(owner) == [("101", 2, False)]
    assert assess_writes(app_url, "wwwwwww4") == 1
    assert limit(owner) == [("101", 3, True)]
    # The third is still open: writing it again is not a fourth, and the open one stands.
    assert assess_writes(app_url, "wwwwwww5") == 0
    owner.execute(
        "UPDATE items SET closed_at = now(), resolution = 'rejected' WHERE id = 'wwwwwww4'"
    )

    with pytest.raises(psycopg.Error, match="three work items") as refused:
        assess_writes(app_url, "wwwwwww6")
    assert refused.value.sqlstate == "BRD04"
    assert owner.execute("SELECT count(*) FROM items").fetchone() == (3,)
    # Another issue is not at its limit.
    assert assess_writes(app_url, "wwwwwww7", 102) == 1


@pytest.mark.parametrize("ended", ["done", "rejected", "superseded"])
def test_a_work_item_counts_however_it_ended(
    app_url: str, owner: psycopg.Connection, ended: str
) -> None:
    for item_id in ("wwwwwww2", "wwwwwww3", "wwwwwww4"):
        insert_stage_row(
            owner, item_id, created_by="run:20260101-assess-09", closed_at="now", resolution=ended
        )
    with pytest.raises(psycopg.Error) as refused:
        assess_writes(app_url, "wwwwwww5")
    assert refused.value.sqlstate == "BRD04"


def test_only_work_items_a_run_wrote_for_the_issue_count(
    app_url: str, owner: psycopg.Connection
) -> None:
    # Continuation items are about the PR and the reconciler's; a session's is not assess's.
    for number, item_id in enumerate(("wwwwwww2", "wwwwwww3", "wwwwwww4"), start=202):
        insert_stage_row(
            owner,
            item_id,
            subject_type="pr",
            subject_ref=str(number),
            created_by=RECONCILER.identity,
            closed_at="now",
            resolution="done",
        )
    for item_id in ("wwwwwww5", "wwwwwww6", "wwwwwww7"):
        worked(owner, item_id, by=SESSION.identity)
    worked(owner, "wwwwwww8", 102)
    for item_id in ("xxxxxxx2", "xxxxxxx3", "xxxxxxx4"):
        insert_stage_row(
            owner,
            item_id,
            assignee="review",
            created_by="run:20260101-assess-09",
            closed_at="now",
            resolution="done",
        )
    assert limit(owner) == [("102", 1, False)]
    assert assess_writes(app_url, "wwwwwww9") == 1


def test_the_limit_is_for_a_run_s_work_item_only(app_url: str, owner: psycopg.Connection) -> None:
    for item_id in ("wwwwwww2", "wwwwwww3", "wwwwwww4"):
        worked(owner, item_id)
    with as_tier(app_url, SESSION) as conn:
        session_id = SESSION.identity.partition(":")[2]
        written = conn.execute(WORK, ("wwwwwww5", REPO, "101", SESSION.identity, session_id))
        assert written.rowcount == 1


def test_the_count_starts_again_when_the_operator_acts(
    app_url: str, owner: psycopg.Connection
) -> None:
    for item_id in ("wwwwwww2", "wwwwwww3", "wwwwwww4"):
        worked(owner, item_id)
    insert_row(owner, id=FIRST)
    insert_row(owner, id=SECOND, subject_ref="102")
    close(app_url, SECOND)
    assert limit(owner) == [("101", 3, True)]

    close(app_url, FIRST, "answered", result={"answer": "try once more"})
    assert limit(owner) == []
    assert assess_writes(app_url, "wwwwwww5") == 1
    assert limit(owner) == [("101", 1, False)]


def test_the_count_starts_again_on_an_approval(app_url: str, owner: psycopg.Connection) -> None:
    for item_id in ("wwwwwww2", "wwwwwww3", "wwwwwww4"):
        worked(owner, item_id)
    insert_pr_item(owner, FIRST, claimed_by=RECONCILER.identity)
    close(app_url, FIRST, by=RECONCILER, result={"cause": "approved"})
    assert assess_writes(app_url, "wwwwwww5") == 1


# ---- The reconciler's heartbeat ----


def test_the_reconciler_records_its_pass(app_url: str, owner: psycopg.Connection) -> None:
    with as_tier(app_url, RECONCILER) as conn:
        first = conn.execute(BEAT).fetchone()
    with as_tier(app_url, RECONCILER) as conn:
        second = conn.execute(BEAT).fetchone()
    assert first is not None and second is not None
    assert second[0] > first[0]
    assert owner.execute("SELECT process, beat_at FROM heartbeats").fetchall() == [
        ("reconciler", second[0])
    ]


@pytest.mark.parametrize("caller", [NIGHTLY, DISPATCHER, SESSION, RUN, OPERATOR])
def test_only_the_reconciler_records_a_pass(
    app_url: str, owner: psycopg.Connection, caller: Caller
) -> None:
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        conn.execute(BEAT)
    assert owner.execute("SELECT count(*) FROM heartbeats").fetchone() == (0,)


# ---- Whose functions these are ----


def test_the_board_s_functions_are_the_owner_s_and_one_is_called_by_a_tier(
    owner: psycopg.Connection,
) -> None:
    rows = owner.execute(
        "SELECT proname, prosecdef, proconfig, pg_get_userbyid(proowner), proacl::text "
        "FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = ANY(%s) "
        "ORDER BY proname",
        (
            [
                "board_wake_assess",
                "item_issues",
                "items_note_operator_act",
                "items_carry_pending_wake",
                "items_refuse_past_fix_forward_limit",
                "items_take_pending_reasons",
                "items_wake_assess",
                "record_heartbeat",
                "replies_note_operator_act",
            ],
        ),
    ).fetchall()
    path = ["search_path=pg_catalog, pg_temp"]
    mine = "board_owner=X/board_owner"
    assert rows == [
        ("board_wake_assess", False, path, "board_owner", f"{{{mine}}}"),
        ("item_issues", False, path, "board_owner", f"{{{mine}}}"),
        ("items_carry_pending_wake", True, path, "board_owner", f"{{{mine}}}"),
        ("items_note_operator_act", True, path, "board_owner", f"{{{mine}}}"),
        # A trigger's function that runs as the caller, like the one for a waiting item.
        ("items_refuse_past_fix_forward_limit", False, path, "board_owner", None),
        # It only moves the row's own reasons, for whoever unclaims the item.
        ("items_take_pending_reasons", False, path, "board_owner", None),
        ("items_wake_assess", True, path, "board_owner", f"{{{mine}}}"),
        ("record_heartbeat", True, path, "board_owner", f"{{{mine},board_process=X/board_owner}}"),
        ("replies_note_operator_act", True, path, "board_owner", f"{{{mine}}}"),
    ]


@pytest.mark.parametrize("caller", [*EVERY_TIER, RUN], ids=lambda c: c.identity)
def test_no_tier_wakes_assess_itself(
    app_url: str, owner: psycopg.Connection, caller: Caller
) -> None:
    calls: list[LiteralString] = [
        "SELECT board_wake_assess('example/repo', '101', ARRAY['operator-items-closed'])",
        "SELECT items_wake_assess()",
        "SELECT items_carry_pending_wake()",
        "SELECT items_note_operator_act()",
        "SELECT replies_note_operator_act()",
        "SELECT * FROM item_issues((SELECT i FROM items AS i LIMIT 1))",
    ]
    for call in calls:
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(call)
