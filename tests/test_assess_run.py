"""An assess run: its result, the work item it writes, the items it reads, and the dated
item the board writes from a `wait`.

The board, the items and the claim helper are test_server.py's. Every repo, number and run
here is invented.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import test_server
from fastmcp.exceptions import ToolError
from helpers import REPO
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from test_run_tools import PASSED, as_run
from test_server import (
    AS_OPERATOR,
    AS_RECONCILER,
    AS_SESSION,
    ASSESS,
    DECISION,
    MANUAL,
    REVIEW,
    TICKET,
    Board,
    claim,
    staged,
)

from factory_board.auth import Identity
from factory_board.kinds import AssessResult, IssueWork

board = test_server.board

RUBRIC: list[dict[str, Any]] = [
    {"check": name, "outcome": "pass", "note": None}
    for name in ("verifiable", "one-behaviour", "prs-alone", "located")
]
UNMET = {"item": "1", "state": "unmet", "evidence": "the widgets Application"}
MET = UNMET | {"state": "met"}
NOT_YET = UNMET | {"state": "cannot-judge"}
NONE: dict[str, Any] = {
    "kind": "assess",
    "outcome": "none",
    "summary": "An open work item covers the issue.",
    "done_when": [UNMET],
    "rubric": RUBRIC,
    "work": None,
    "items": [],
    "wait": None,
    "comment": None,
}
SESSION_ITEM: dict[str, Any] = {
    "kind": "session",
    "ticket": TICKET,
    "summary": "The issue needs a plan.",
    "prompt": "Plan the widget press move.",
}
PR_ASSESS: dict[str, Any] = {"kind": "assess", "pr": {"repo": REPO, "number": 202}}
FILES = ["press/config.yaml", "docs/press.md"]


def ahead(**delta: int) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


def waiting(when: str, note: str = "The soak has run a week.") -> dict[str, Any]:
    return NONE | {
        "outcome": "wait",
        "done_when": [NOT_YET],
        "wait": {"not_before": when, "note": note},
    }


def failed(check: str = "located") -> list[dict[str, Any]]:
    return [
        row | {"outcome": "fail", "note": "No file is named."} if row["check"] == check else row
        for row in RUBRIC
    ]


async def assessing(
    board: Board, number: int = 1, item: dict[str, Any] = ASSESS, **claimed: Any
) -> tuple[Identity, str]:
    """An assess item, claimed for a run: the run as a caller, and the item's ID."""
    run_id = f"20260101-assess-0{number}"
    # The board may have written the issue's assess item itself, on a wake.
    written = await board.call(AS_RECONCILER, "write_stage_item", item=item)
    item_id = written["item"]["id"]
    if "issue" in item:
        # An assess item on an issue is claimed with the issue's hash.
        claimed = {"input_hash": "hash-1"} | claimed
    await claim(board, item_id, run_id, **claimed)
    return as_run(run_id, item_id, "assess"), item_id


async def pipeline(board: Board) -> list[dict[str, Any]]:
    return (await board.call(AS_RECONCILER, "list_pipeline_items"))["items"]


# ---- the models ----


def test_an_assess_result_fits_its_outcome() -> None:
    assert AssessResult.model_validate(NONE).model_dump(mode="json") == NONE
    bare = {k: v for k, v in NONE.items() if k in ("outcome", "summary", "rubric")}
    assert AssessResult.model_validate(bare).done_when == ()
    assert AssessResult.model_validate(NONE | {"outcome": "work", "work": "abcdefgh"}).work
    assert AssessResult.model_validate(waiting(ahead(days=7))).wait is not None
    closed = NONE | {"outcome": "close", "done_when": [MET], "comment": 4_000_000_001}
    assert AssessResult.model_validate(closed).comment == 4_000_000_001


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (NONE | {"outcome": "work"}, "work needs `work`"),
        (NONE | {"work": "abcdefgh"}, "`work` is for the outcome work only"),
        (NONE | {"outcome": "work", "work": "abcdefgh", "rubric": failed()}, "no failed check"),
        (NONE | {"rubric": RUBRIC[:3]}, "rubric"),
        (NONE | {"rubric": [*RUBRIC[:3], RUBRIC[0]]}, "every check once"),
        (NONE | {"rubric": [*RUBRIC[:3], RUBRIC[3] | {"outcome": "fail"}]}, "needs a `note`"),
        (NONE | {"outcome": "operator"}, "operator needs `items`"),
        (NONE | {"outcome": "plan"}, "plan needs `items`"),
        (NONE | {"items": ["abcdefgh"]}, "none writes nothing"),
        (NONE | {"outcome": "operator", "items": ["abcdefgh", "abcdefgh"]}, "named twice"),
        (NONE | {"outcome": "wait"}, "wait needs `wait`"),
        (waiting(ahead(days=7)) | {"done_when": [UNMET]}, "can't be judged yet"),
        (waiting("2026-01-31T08:00:00"), "not_before"),
        (NONE | {"outcome": "close", "done_when": [MET]}, "close needs `comment`"),
        (NONE | {"outcome": "close", "comment": 7}, "every done-when item met"),
        (NONE | {"outcome": "close", "comment": 7, "done_when": [MET, UNMET]}, "every done-when"),
        (NONE | {"outcome": "done"}, "outcome"),
        (NONE | {"verdict": "pass"}, "verdict"),
    ],
)
def test_an_assess_result_that_does_not_fit_is_refused(
    result: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        AssessResult.model_validate(result)


def test_an_issue_s_work_item_names_a_repo_and_its_files() -> None:
    work = IssueWork.model_validate({"issue": TICKET, "repo": REPO, "files": FILES})
    assert (work.kind, work.subject.label, work.evidence) == ("work", f"issue {REPO}#101", ())
    no_files: list[str] = []
    for files in (no_files, ["a"] * 2, ["../a"], ["/a"], ["a/"], ["a//b"], ["a b"], ["f"] * 31):
        with pytest.raises(ValidationError, match="file"):
            IssueWork.model_validate({"issue": TICKET, "repo": REPO, "files": files})


# ---- the work item ----


async def test_an_assess_run_writes_its_issue_s_work_item_and_closes_with_it(
    board: Board,
) -> None:
    run, item_id = await assessing(board, input_hash="hash-1")
    written = await board.call(
        run, "write_work_item", repo=REPO, files=FILES, evidence=["the widgets Application"]
    )
    work = written["item"]
    assert (written["status"], work["kind"], work["subject"]) == (
        "created",
        "work",
        f"issue {REPO}#101",
    )
    assert (work["held_by"], work["created_by"], work["input_hash"]) == (
        None,
        "run:20260101-assess-01",
        "hash-1",
    )
    assert work["fields"] == {
        "kind": "work",
        "issue": TICKET,
        "repo": REPO,
        "files": FILES,
        "evidence": ["the widgets Application"],
    }
    # A second write answers with the open one.
    again = await board.call(run, "write_work_item", repo=REPO, files=["other.md"])
    assert (again["status"], again["item"]["id"]) == ("already open", work["id"])
    assert "outcome none" in again["note"]

    # The close names a work item this run wrote, for this issue.
    theirs = await board.create(DECISION)
    for named in (theirs, "abcdefgh"):
        with pytest.raises(ToolError, match=r"not a work item this run wrote|no item"):
            await board.call(run, "close_my_item", result=NONE | {"outcome": "work", "work": named})
    result = NONE | {"outcome": "work", "work": work["id"]}
    closed = await board.call(run, "close_my_item", result=result)
    assert (closed["item"]["id"], closed["item"]["result"]) == (item_id, result)
    # The work item waits in the queue; nothing else was written.
    assert [(entry["kind"], entry["open"]) for entry in await pipeline(board)] == [("work", True)]
    queue = await board.call(test_server.AS_DISPATCHER, "list_claimable")
    assert [entry["kind"] for entry in queue["items"]] == ["work"]


async def test_the_work_item_tool_is_an_assess_run_s_on_an_issue(board: Board) -> None:
    review_id = await staged(board, REVIEW)
    await claim(board, review_id, "20260101-review-01")
    reviewer = as_run("20260101-review-01", review_id, "review")
    for tool, arguments in (
        ("write_work_item", {"repo": REPO, "files": FILES}),
        ("my_issue_items", {}),
    ):
        with pytest.raises(ToolError, match=f"{tool} is an assess run's tool, not a review run's"):
            await board.call(reviewer, tool, **arguments)
    with pytest.raises(ToolError, match="a review run closes with a review result"):
        await board.call(reviewer, "close_my_item", result=NONE)

    on_pr, _ = await assessing(board, 2, PR_ASSESS)
    with pytest.raises(ToolError, match="about a PR: a work item needs an issue"):
        await board.call(on_pr, "write_work_item", repo=REPO, files=FILES)
    with pytest.raises(ToolError, match="an assess run closes with an assess result"):
        await board.call(on_pr, "close_my_item", result=PASSED)
    assert [entry["kind"] for entry in await pipeline(board)] == ["review", "assess"]


async def test_the_board_refuses_a_fourth_work_item_through_the_tool(
    board: Board, conn: Any
) -> None:
    for number in (1, 2, 3):
        run, _ = await assessing(board, number)
        work = (await board.call(run, "write_work_item", repo=REPO, files=FILES))["item"]["id"]
        await board.call(run, "close_my_item", result=NONE | {"outcome": "work", "work": work})
        # The work is over, however it ended.
        conn.execute(
            "UPDATE items SET closed_at = now(), resolution = 'superseded' WHERE id = %s", (work,)
        )
    run, _ = await assessing(board, 4)
    with pytest.raises(ToolError, match=r"three work items.*decision item.*outcome operator"):
        await board.call(run, "write_work_item", repo=REPO, files=FILES)
    # The way on: a decision item with the evidence, and a close as operator.
    decision = (await board.call(run, "create_item", item=DECISION))["item"]["id"]
    result = NONE | {"outcome": "operator", "items": [decision]}
    assert (await board.call(run, "close_my_item", result=result))["status"] == "closed"


# ---- the operator's items ----


async def test_an_assess_run_writes_decision_and_session_items_only(board: Board) -> None:
    run, _ = await assessing(board)
    with pytest.raises(ToolError, match="an assess run writes decision or session items, not a"):
        await board.call(run, "create_item", item=MANUAL)
    decision = (await board.call(run, "create_item", item=DECISION))["item"]["id"]
    session = (await board.call(run, "create_item", item=SESSION_ITEM))["item"]["id"]
    theirs = await board.create(DECISION)
    loose = (await board.call(run, "create_item", item=DECISION, holds=False))["item"]["id"]

    def result(outcome: str, *items: str) -> dict[str, Any]:
        return NONE | {"outcome": outcome, "items": list(items)}

    for refused, message in (
        (result("operator", theirs), "not an item this run wrote for the operator"),
        (result("operator", "abcdefgh"), "no item"),
        (result("plan", decision), "plan needs a session item"),
        (result("operator", loose), "operator needs an item that holds the issue"),
    ):
        with pytest.raises(ToolError, match=message):
            await board.call(run, "close_my_item", result=refused)
    assert (await board.call(run, "my_item"))["open"]
    closed = await board.call(run, "close_my_item", result=result("plan", decision, session))
    assert closed["item"]["result"]["items"] == [decision, session]


# ---- the wait ----


async def test_a_wait_makes_the_board_write_the_dated_assess_item(board: Board) -> None:
    run, item_id = await assessing(board)
    for when, message in (
        (ahead(minutes=-1), "is ahead"),
        (ahead(days=91), "at most 90 days ahead"),
    ):
        with pytest.raises(ToolError, match=message):
            await board.call(run, "close_my_item", result=waiting(when))
    when = ahead(days=7)
    await board.call(run, "close_my_item", result=waiting(when))
    (dated,) = await pipeline(board)
    assert dated["id"] != item_id
    assert (dated["kind"], dated["subject"], dated["reasons"], dated["created_by"]) == (
        "assess",
        f"issue {REPO}#101",
        ["dated"],
        "process:board",
    )
    assert datetime.fromisoformat(dated["deferred_until"]) == datetime.fromisoformat(when)
    assert dated["fields"] == {
        "kind": "assess",
        "issue": TICKET,
        "pr": None,
        "note": "The soak has run a week.",
    }
    # It is in no queue before its date.
    assert (await board.call(test_server.AS_DISPATCHER, "list_claimable"))["items"] == []


async def test_a_wake_that_waited_keeps_the_dated_item_due_now(board: Board) -> None:
    mine = await board.create(MANUAL)
    run, _ = await assessing(board)
    # The operator's last item on the issue closes while the run holds the assess item.
    await board.call(AS_OPERATOR, "done", item_id=mine)
    assert (await board.call(run, "my_item"))["pending_reasons"] == ["operator-items-closed"]
    await board.call(run, "close_my_item", result=waiting(ahead(days=7), "Look at the press."))
    (woken,) = await pipeline(board)
    assert (woken["reasons"], woken["deferred_until"], woken["fields"]["note"]) == (
        ["operator-items-closed", "dated"],
        None,
        "Look at the press.",
    )


async def test_a_wait_on_a_pr_dates_the_pr_s_assess_item(board: Board) -> None:
    run, _ = await assessing(board, 1, PR_ASSESS)
    await board.call(run, "close_my_item", result=waiting(ahead(days=1)))
    (dated,) = await pipeline(board)
    assert (dated["subject"], dated["fields"]["pr"], dated["fields"]["issue"]) == (
        f"pr {REPO}#202",
        {"repo": REPO, "number": 202},
        None,
    )


# ---- what the run reads ----


async def test_an_assess_run_reads_the_items_about_its_issue(board: Board) -> None:
    done = await board.create(MANUAL)
    await board.call(AS_OPERATOR, "reply", item_id=done, text="The press was restocked.")
    elsewhere = await board.create(DECISION | {"ticket": {"repo": REPO, "number": 102}})
    approve = (
        await board.call(
            AS_SESSION,
            "create_item",
            item=test_server.PR_APPROVE | {"ticket": TICKET},
        )
    )["item"]["id"]
    await staged(board, REVIEW)
    run, item_id = await assessing(board)
    shown = await board.call(run, "my_issue_items")
    assert (shown["subject"], shown["more"]) == (f"issue {REPO}#101", False)
    listed = {entry["id"]: entry for entry in shown["items"]}
    # The issue's own items and the PR item that names it as its ticket; not another
    # issue's, and not a stage's item about the PR.
    assert set(listed) == {done, approve, item_id}
    assert elsewhere not in listed
    assert (listed[done]["open"], listed[done]["resolution"]) == (False, "replied")
    assert [reply["text"] for reply in listed[done]["replies"]] == ["The press was restocked."]
    assert listed[approve]["kind"] == "pr-approve"


async def test_the_list_is_the_newest_fifty(board: Board, conn: Any) -> None:
    run, item_id = await assessing(board)
    conn.execute(
        "INSERT INTO items (id, assignee, kind, subject_repo, subject_type, subject_ref, "
        "claimed_by, created_by, fields, created_at) "
        "SELECT board_new_item_id(), 'operator', 'decision', %s, 'issue', '101', "
        "'human:operator', 'session:0a1b2c3d', %s, now() - make_interval(days => 60 - n) "
        "FROM generate_series(1, 55) AS n",
        (REPO, Jsonb(DECISION)),
    )
    shown = await board.call(run, "my_issue_items")
    assert (len(shown["items"]), shown["more"]) == (50, True)
    # The run's own item is the newest; the oldest six are left out.
    assert shown["items"][-1]["id"] == item_id
    assert all(entry["kind"] == "decision" for entry in shown["items"][:-1])
