"""What the board does by itself, through the store and the tools: the wake, the fix-forward
limit, the heartbeat and the queue's metrics.

The rules themselves are tested with plain SQL in test_board_triggers_sql.py. Every repo,
number, run and command here is invented.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from helpers import (
    DISPATCHER,
    NIGHTLY,
    OPERATOR,
    RECONCILER,
    REPO,
    RUN,
    SESSION,
    SHA_A,
    SHA_B,
    manual,
    pr,
    stage,
)
from prometheus_client import REGISTRY
from pydantic import ValidationError

from factory_board import metrics
from factory_board.auth import Identity
from factory_board.kinds import Assess, AssessReason, DeployCheck, parse_stage_item
from factory_board.server import build_server
from factory_board.store import (
    Caller,
    ClaimCount,
    FixForwardLimit,
    NotPermitted,
    QueueCount,
    Resolution,
    Store,
    Subject,
    Tier,
)

AS_OPERATOR = Identity(OPERATOR, operator_typed=True)
AS_SESSION = Identity(SESSION, operator_typed=True)
AS_NIGHTLY = Identity(NIGHTLY, operator_typed=False)
AS_RECONCILER = Identity(RECONCILER, operator_typed=False)
AS_DISPATCHER = Identity(DISPATCHER, operator_typed=False)
METRICS = Caller(Tier.PROCESS, "process:metrics")

TICKET = {"repo": REPO, "number": 101}
MANUAL: dict[str, Any] = {
    "kind": "manual",
    "ticket": TICKET,
    "summary": "Restock the widget press.",
    "command": "make widgets",
    "machine": "workstation",
}
PR_APPROVE: dict[str, Any] = {
    "kind": "pr-approve",
    "ticket": TICKET,
    "summary": "Touches the widget press.",
    "repo": REPO,
    "number": 202,
    "head_sha": SHA_A,
    "machine": "browser",
}
DEPLOY_CHECK: dict[str, Any] = {
    "kind": "deploy-check",
    "repo": REPO,
    "number": 202,
    "merge_sha": SHA_B,
    "issues": [TICKET, {"repo": REPO, "number": 102}],
}


class Board:
    """The server with a caller the test switches."""

    def __init__(self, store: Store) -> None:
        self.identity = AS_SESSION
        self.server = build_server(store, self.who, "operator")

    async def who(self) -> Identity:
        return self.identity

    async def call(self, identity: Identity, tool: str, **arguments: Any) -> dict[str, Any]:
        self.identity = identity
        async with Client(self.server) as client:
            return (await client.call_tool(tool, arguments)).data

    async def create(self, item: dict[str, Any], **arguments: Any) -> str:
        created = await self.call(AS_SESSION, "create_item", item=item, **arguments)
        assert created["status"] == "created", created
        return created["item"]["id"]

    async def assessments(self) -> list[tuple[str, list[str], str]]:
        """The open assess items, by subject: (subject, reasons, creator)."""
        listed = await self.call(AS_RECONCILER, "list_pipeline_items")
        return sorted(
            (entry["subject"], entry["reasons"], entry["created_by"])
            for entry in listed["items"]
            if entry["kind"] == "assess"
        )


@pytest.fixture
def board(store: Store) -> Board:
    return Board(store)


def sample(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


# ---- the wake, through the operator's verbs ----


async def test_done_on_the_last_item_writes_the_issue_s_assess_item(board: Board) -> None:
    first = await board.create(MANUAL)
    second = await board.create(MANUAL | {"summary": "Then oil it."})
    await board.call(AS_OPERATOR, "done", item_id=first)
    assert await board.assessments() == []
    await board.call(AS_OPERATOR, "done", item_id=second)
    assert await board.assessments() == [
        (f"issue {REPO}#101", ["operator-items-closed"], "process:board")
    ]
    # It is in the dispatcher's queue at once, as any assess item.
    queue = await board.call(AS_DISPATCHER, "list_claimable")
    assert [(entry["kind"], entry["rank"]) for entry in queue["items"]] == [("assess", 6)]


async def test_a_reject_and_a_closing_reply_say_what_they_woke(board: Board) -> None:
    first = await board.create(MANUAL)
    waiting = await board.create(MANUAL | {"ticket": {"repo": REPO, "number": 102}}, after=[first])
    rejected = await board.call(AS_OPERATOR, "reject", item_id=first, reason="the press is sold")
    assert [entry["id"] for entry in rejected["also_closed"]] == [waiting]
    assert "written the issue's assess item" in rejected["note"]
    assert await board.assessments() == [
        (f"issue {REPO}#101", ["operator-items-closed"], "process:board"),
        (f"issue {REPO}#102", ["operator-items-closed"], "process:board"),
    ]

    third = await board.create(MANUAL | {"ticket": {"repo": REPO, "number": 103}})
    replied = await board.call(AS_OPERATOR, "reply", item_id=third, text="did it by hand")
    assert "written the issue's assess item" in replied["note"]
    assert len(await board.assessments()) == 3


async def test_an_item_that_does_not_hold_wakes_nothing(board: Board) -> None:
    loose = await board.create(MANUAL, holds=False)
    await board.call(AS_OPERATOR, "reject", item_id=loose, reason="not needed")
    assert await board.assessments() == []


async def test_a_second_trigger_from_the_reconciler_merges_into_the_board_s_item(
    board: Board,
) -> None:
    await board.call(AS_OPERATOR, "done", item_id=await board.create(MANUAL))
    again = await board.call(
        AS_RECONCILER,
        "write_stage_item",
        item={"kind": "assess", "issue": TICKET},
        reasons=["changed"],
    )
    assert again["status"] == "already open"
    assert again["item"]["reasons"] == ["operator-items-closed", "changed"]


async def test_a_rejected_pr_item_wakes_its_ticket_s_issue(board: Board) -> None:
    written = await board.call(AS_RECONCILER, "write_operator_item", item=PR_APPROVE)
    await board.call(
        AS_RECONCILER, "close_for_fact", item_id=written["item"]["id"], cause="head-moved"
    )
    assert await board.assessments() == []
    moved = await board.call(
        AS_RECONCILER, "write_operator_item", item=PR_APPROVE | {"head_sha": SHA_B}
    )
    await board.call(AS_OPERATOR, "reject", item_id=moved["item"]["id"], reason="not like this")
    assert await board.assessments() == [
        (f"issue {REPO}#101", ["operator-items-closed"], "process:board")
    ]


# ---- the wake, from a deploy-check ----


async def test_a_deploy_check_names_its_issues(board: Board) -> None:
    parsed = parse_stage_item(DEPLOY_CHECK)
    assert isinstance(parsed, DeployCheck)
    assert [(issue.repo, issue.number) for issue in parsed.issues] == [(REPO, 101), (REPO, 102)]
    bare = parse_stage_item({k: v for k, v in DEPLOY_CHECK.items() if k != "issues"})
    assert isinstance(bare, DeployCheck) and bare.issues == ()
    for bad in ([TICKET, TICKET], [{"repo": REPO}], [TICKET] * 11, "101"):
        with pytest.raises(ValidationError):
            parse_stage_item(DEPLOY_CHECK | {"issues": bad})
    with pytest.raises(ToolError):
        await board.call(
            AS_RECONCILER, "write_stage_item", item=DEPLOY_CHECK | {"issues": [TICKET, TICKET]}
        )


PR_ASSESS: dict[str, Any] = {"kind": "assess", "pr": {"repo": REPO, "number": 202}}


def test_an_assess_item_is_about_an_issue_or_a_pr() -> None:
    on_issue = parse_stage_item({"kind": "assess", "issue": TICKET})
    on_pr = parse_stage_item(PR_ASSESS)
    assert isinstance(on_issue, Assess) and isinstance(on_pr, Assess)
    assert (on_issue.subject.label, on_pr.subject.label) == (
        f"issue {REPO}#101",
        f"pr {REPO}#202",
    )
    assert on_pr.model_dump(mode="json") == {
        "kind": "assess",
        "issue": None,
        "pr": {"repo": REPO, "number": 202},
        "note": None,
    }
    for bad in (
        {"kind": "assess"},
        {"kind": "assess", "issue": None, "pr": None},
        PR_ASSESS | {"issue": TICKET},
        {"kind": "assess", "pr": {"repo": REPO, "number": 0}},
        {"kind": "assess", "pr": {"repo": REPO}},
        {"kind": "assess", "pr": 202},
    ):
        with pytest.raises(ValidationError):
            parse_stage_item(bad)


async def test_an_assess_item_on_a_pr_is_listed_and_queued_like_any(
    board: Board, conn: psycopg.Connection
) -> None:
    written = await board.call(
        AS_RECONCILER, "write_stage_item", item=PR_ASSESS, reasons=["deploy-checks-closed"]
    )
    item = written["item"]
    assert (written["status"], item["subject"]) == ("created", f"pr {REPO}#202")
    assert written["confirmation"] == f"assess for pr {REPO}#202"
    assert item["fields"] == {
        "kind": "assess",
        "issue": None,
        "pr": {"repo": REPO, "number": 202},
        "note": None,
    }
    again = await board.call(AS_RECONCILER, "write_stage_item", item=PR_ASSESS, reasons=["changed"])
    assert (again["status"], again["item"]["id"]) == ("already open", item["id"])
    assert await board.assessments() == [
        (f"pr {REPO}#202", ["deploy-checks-closed", "changed"], RECONCILER.identity)
    ]
    queue = await board.call(AS_DISPATCHER, "list_claimable")
    assert [(entry["id"], entry["rank"]) for entry in queue["items"]] == [(item["id"], 6)]
    # No staleness hash is asked for: the hash is an issue's.
    claimed = await board.call(
        AS_DISPATCHER,
        "claim",
        item_id=item["id"],
        run_id="20260101-assess-01",
        cap_minutes=30,
        seat="cairo",
        seat_type="normal",
    )
    assert claimed["status"] == "claimed"
    # Closed, it is no issue's last assessment.
    conn.execute(
        "UPDATE items SET closed_at = now(), resolution = 'done' WHERE id = %s", (item["id"],)
    )
    assert (await board.call(AS_RECONCILER, "list_assessments", repo=REPO))["items"] == []


async def test_a_deploy_check_closed_for_a_fact_wakes_its_issues(board: Board) -> None:
    written = await board.call(AS_RECONCILER, "write_stage_item", item=DEPLOY_CHECK)
    assert written["item"]["fields"]["issues"] == DEPLOY_CHECK["issues"]
    assert await board.assessments() == []
    await board.call(AS_RECONCILER, "close_for_fact", item_id=written["item"]["id"], cause="gone")
    assert await board.assessments() == [
        (f"issue {REPO}#101", ["deploy-checks-closed"], "process:board"),
        (f"issue {REPO}#102", ["deploy-checks-closed"], "process:board"),
    ]


async def test_a_deploy_check_written_closed_wakes_its_issues(board: Board) -> None:
    record = await board.call(
        AS_RECONCILER, "write_stage_item", item=DEPLOY_CHECK, closed_as="nothing-to-deploy"
    )
    assert record["item"]["open"] is False
    assert [entry[:2] for entry in await board.assessments()] == [
        (f"issue {REPO}#101", ["deploy-checks-closed"]),
        (f"issue {REPO}#102", ["deploy-checks-closed"]),
    ]


# ---- the fix-forward limit ----


RUN_ID = RUN.identity.partition(":")[2]


def assess_writes(conn: psycopg.Connection, number: int = 101) -> str:
    """An assess run's work item on the issue, held by the run; its ID.

    Written in the run's name past the store: no run inserts a stage's item until assess
    gets its tools (0012). The limit is the trigger's, which reads `created_by`.
    """
    row = conn.execute(
        "INSERT INTO items (id, assignee, kind, subject_repo, subject_type, subject_ref, "
        "claimed_by, created_by, created_run) "
        "VALUES (board_new_item_id(), 'work', 'work', %s, 'issue', %s, %s, %s, %s) RETURNING id",
        (REPO, str(number), RUN.identity, RUN.identity, RUN_ID),
    ).fetchone()
    assert row is not None
    return row[0]


async def test_the_store_refuses_a_fourth_work_item_from_a_run(
    store: Store, conn: psycopg.Connection
) -> None:
    for _ in range(2):
        await store.close_item(RUN, assess_writes(conn), Resolution.DONE)
    third = assess_writes(conn)
    await store.close_item(OPERATOR, third, Resolution.REJECTED, {"reason": "stop"})

    # The reject was the operator's act on the issue, so the count starts again.
    for _ in range(3):
        await store.close_item(RUN, assess_writes(conn), Resolution.DONE)
    # The limit is checked before the rule that keeps a run from the insert: the run is
    # told the limit.
    with pytest.raises(FixForwardLimit, match="three work items"):
        await store.create_item(RUN, stage("work", 101, held_by=RUN.identity))
    assert (await store.stats(METRICS)).at_fix_forward_limit == 1

    # Another issue, a session's work item and a continuation item are not refused.
    assess_writes(conn, 102)
    assert (await store.create_item(SESSION, stage("work", 103))).existing is False
    continuation = replace(stage("work"), subject=pr(202))
    assert (await store.create_item(RECONCILER, continuation)).existing is False

    # The operator's own item on the issue, once closed, frees it.
    mine = (await store.create_item(SESSION, manual(101))).item
    await store.close_item(OPERATOR, mine.id, Resolution.DONE)
    assess_writes(conn)
    assert (await store.stats(METRICS)).at_fix_forward_limit == 0


# ---- the heartbeat ----


async def test_the_reconciler_records_a_pass_and_nobody_else(board: Board, store: Store) -> None:
    assert (await store.stats(METRICS)).reconciler_pass is None
    metrics.show(await store.stats(METRICS))
    assert sample("factory_board_reconciler_pass_timestamp_seconds") == 0
    before = datetime.now(UTC) - timedelta(seconds=5)
    beat = await board.call(AS_RECONCILER, "heartbeat")
    assert beat["status"] == "recorded"
    at = datetime.fromisoformat(beat["at"])
    assert before < at < datetime.now(UTC) + timedelta(seconds=5)
    recorded = (await store.stats(METRICS)).reconciler_pass
    assert recorded is not None and abs(recorded - at.timestamp()) < 0.001

    later = datetime.fromisoformat((await board.call(AS_RECONCILER, "heartbeat"))["at"])
    assert later > at

    for identity in (AS_OPERATOR, AS_SESSION, AS_NIGHTLY, AS_DISPATCHER):
        with pytest.raises(ToolError, match="not a tool of this caller's tier"):
            await board.call(identity, "heartbeat")
    for caller in (NIGHTLY, DISPATCHER, SESSION, OPERATOR):
        with pytest.raises(NotPermitted):
            await store.record_heartbeat(caller)

    metrics.show(await store.stats(METRICS))
    exported = sample("factory_board_reconciler_pass_timestamp_seconds")
    assert exported is not None and abs(exported - later.timestamp()) < 0.001


# ---- the queue's metrics ----


async def test_the_stats_count_the_queue_and_the_claims(
    store: Store, conn: psycopg.Connection
) -> None:
    review = (await store.create_item(RECONCILER, replace(stage("review"), subject=pr(202)))).item
    urgent = replace(stage("review"), subject=pr(203), urgent=True)
    await store.create_item(RECONCILER, urgent)
    held = (await store.create_item(RECONCILER, stage("assess", 101))).item
    await store.create_item(RECONCILER, stage("assess", 102))
    # Not in the queue: a dated item, a waiting one, a merge item, the operator's.
    dated = replace(stage("assess", 103), not_before=datetime.now(UTC) + timedelta(days=1))
    await store.create_item(RECONCILER, dated)
    blocker = (await store.create_item(SESSION, manual(104))).item
    await store.create_item(RECONCILER, replace(stage("work", 104), after=(blocker.id,)))
    await store.create_item(RECONCILER, replace(stage("merge"), subject=pr(204)))

    def queue(stats: Any) -> dict[tuple[str, bool], int]:
        return {(q.stage, q.urgent): q.count for q in stats.queue}

    stats = await store.stats(METRICS)
    assert queue(stats) == {("review", False): 1, ("review", True): 1, ("assess", False): 2}
    assert stats.claims == ()
    now = datetime.now(UTC).timestamp()
    assert all(isinstance(q, QueueCount) and abs(q.oldest_since - now) < 300 for q in stats.queue)

    # A claimed item is out of the queue; a released one is back, since its release.
    await store.claim_item(
        DISPATCHER, held.id, "20260101-assess-01", 30, "hash-1", seat="cairo", seat_type="normal"
    )
    await store.claim_item(
        DISPATCHER, review.id, "20260101-review-01", 30, seat="tokyo", seat_type="normal"
    )
    conn.execute("UPDATE items SET created_at = created_at - interval '2 hours'")
    conn.execute("UPDATE claims SET claimed_at = claimed_at - interval '2 hours'")
    await store.release_claim(DISPATCHER, review.id, "usage-limit")
    await store.claim_item(
        DISPATCHER, review.id, "20260101-review-02", 30, seat="seoul", seat_type="normal"
    )
    conn.execute(
        "UPDATE items SET claim_expires = now() - interval '1 minute' WHERE id = %s", (review.id,)
    )
    conn.execute(
        "UPDATE claims SET expires_at = now() - interval '1 minute' WHERE run = %s",
        ("20260101-review-02",),
    )
    await store.expire_claims(DISPATCHER, OPERATOR.identity)

    stats = await store.stats(METRICS)
    assert queue(stats) == {("review", False): 1, ("review", True): 1, ("assess", False): 1}
    since = {(q.stage, q.urgent): q.oldest_since for q in stats.queue}
    # The plain review item was created two hours ago and entered the queue just now.
    assert abs(since[("review", False)] - now) < 300
    assert abs(since[("review", True)] - (now - 7200)) < 300
    assert sorted(stats.claims, key=lambda c: (c.stage, c.ending)) == [
        ClaimCount("assess", "running", "", 1),
        ClaimCount("review", "expired", "", 1),
        ClaimCount("review", "released", "usage-limit", 1),
    ]


async def test_the_metrics_show_the_queue_the_claims_and_the_limit(
    board: Board, store: Store
) -> None:
    metrics.show(await store.stats(METRICS))
    # Every stage of the order has both series, at 0, and no age.
    for stage_name in metrics.QUEUE_STAGES:
        for urgent in ("false", "true"):
            assert sample("factory_board_claimable_items", stage=stage_name, urgent=urgent) == 0
            assert (
                sample(
                    "factory_board_oldest_claimable_item_timestamp_seconds",
                    stage=stage_name,
                    urgent=urgent,
                )
                is None
            )
    assert sample("factory_board_claimable_items", stage="merge", urgent="false") is None
    assert sample("factory_board_issues_at_fix_forward_limit") == 0

    written = await board.call(
        AS_RECONCILER, "write_stage_item", item={"kind": "assess", "issue": TICKET}
    )
    await board.call(
        AS_RECONCILER,
        "write_stage_item",
        item={"kind": "review", "repo": REPO, "number": 202, "head_sha": SHA_A},
        urgent=True,
    )
    await board.call(
        AS_DISPATCHER,
        "claim",
        item_id=written["item"]["id"],
        run_id="20260101-assess-01",
        cap_minutes=30,
        input_hash="hash-1",
        seat="cairo",
        seat_type="normal",
    )
    await board.call(AS_DISPATCHER, "release", item_id=written["item"]["id"], reason="infra-window")
    metrics.show(await store.stats(METRICS))
    assert sample("factory_board_claimable_items", stage="assess", urgent="false") == 1
    assert sample("factory_board_claimable_items", stage="review", urgent="true") == 1
    assert sample("factory_board_claimable_items", stage="review", urgent="false") == 0
    oldest = sample(
        "factory_board_oldest_claimable_item_timestamp_seconds", stage="assess", urgent="false"
    )
    assert oldest is not None and abs(oldest - datetime.now(UTC).timestamp()) < 300
    assert (
        sample("factory_board_claims", stage="assess", ending="released", reason="infra-window")
        == 1
    )
    assert sample("factory_board_claims", stage="assess", ending="running", reason="") is None

    # A queue that empties goes back to 0, and a claim that changes leaves no old series.
    await board.call(
        AS_DISPATCHER,
        "claim",
        item_id=written["item"]["id"],
        run_id="20260101-assess-02",
        cap_minutes=30,
        input_hash="hash-1",
        seat="cairo",
        seat_type="normal",
    )
    metrics.show(await store.stats(METRICS))
    assert sample("factory_board_claimable_items", stage="assess", urgent="false") == 0
    assert sample("factory_board_claims", stage="assess", ending="running", reason="") == 1


# ---- a wake while a run holds the issue's assess item ----


async def held_assess(board: Board, run_id: str = "20260101-assess-01") -> str:
    """Issue 101's assess item, claimed for a run; its ID."""
    written = await board.call(
        AS_RECONCILER, "write_stage_item", item={"kind": "assess", "issue": TICKET}
    )
    item_id = written["item"]["id"]
    await board.call(
        AS_DISPATCHER,
        "claim",
        item_id=item_id,
        run_id=run_id,
        cap_minutes=30,
        input_hash="hash-1",
        seat="cairo",
        seat_type="normal",
    )
    return item_id


async def test_a_wake_during_a_run_is_shown_and_carried_to_the_next_item(
    board: Board, store: Store
) -> None:
    held = await held_assess(board)
    await board.call(AS_OPERATOR, "done", item_id=await board.create(MANUAL))
    await board.call(
        AS_RECONCILER, "write_stage_item", item=DEPLOY_CHECK, closed_as="nothing-to-deploy"
    )
    shown = await board.call(AS_OPERATOR, "get_item", item_id=held)
    assert shown["pending_reasons"] == ["operator-items-closed", "deploy-checks-closed"]
    assert "reasons" not in shown
    waiting = await store.get_item(OPERATOR, held)
    assert waiting.pending_reasons == ("operator-items-closed", "deploy-checks-closed")

    # The run closes its item: the next one is written in that transaction.
    run = Caller(Tier.AGENT, "run:20260101-assess-01")
    closed = await store.close_item(run, held, Resolution.DONE, {"outcome": "wait"})
    assert closed.is_open is False
    assert (await store.get_item(OPERATOR, held)).pending_reasons == ()
    (next_item,) = [
        item
        for item in await store.list_open(OPERATOR, assignee="assess")
        if item.subject == Subject("issue", "101", REPO)
    ]
    assert next_item.reasons == ("operator-items-closed", "deploy-checks-closed")
    assert (next_item.claimed_by, next_item.created_by) == (None, "process:board")
    assert (next_item.not_before, next_item.pending_reasons) == (None, ())


async def test_a_release_gives_the_item_what_arrived_during_the_run(board: Board) -> None:
    held = await held_assess(board)
    await board.call(AS_OPERATOR, "done", item_id=await board.create(MANUAL))
    released = await board.call(AS_DISPATCHER, "release", item_id=held, reason="infra-window")
    assert released["item"]["reasons"] == ["operator-items-closed"]
    assert "pending_reasons" not in released["item"]
    queue = await board.call(AS_DISPATCHER, "list_claimable")
    assert [entry["id"] for entry in queue["items"]] == [held]


async def test_a_run_s_close_with_nothing_pending_writes_nothing(
    board: Board, store: Store
) -> None:
    held = await held_assess(board)
    run = Caller(Tier.AGENT, "run:20260101-assess-01")
    await store.close_item(run, held, Resolution.DONE)
    assert await store.list_open(OPERATOR) == []


# ---- the reasons ----


@pytest.mark.parametrize("reason", [reason.value for reason in AssessReason])
async def test_the_reconciler_writes_every_reason(board: Board, reason: str) -> None:
    written = await board.call(
        AS_RECONCILER,
        "write_stage_item",
        item={"kind": "assess", "issue": TICKET},
        reasons=[reason],
    )
    assert written["item"]["reasons"] == [reason]


async def test_the_reasons_are_these_nine(board: Board) -> None:
    assert [reason.value for reason in AssessReason] == [
        "new",
        "changed",
        "reopened",
        "closed-elsewhere",
        "operator-items-closed",
        "deploy-checks-closed",
        "dated",
        "alert",
        "orphaned",
    ]
    # The sweep's reason merges into an open item like any other, and all nine fit one write.
    await board.call(AS_OPERATOR, "done", item_id=await board.create(MANUAL))
    merged = await board.call(
        AS_RECONCILER,
        "write_stage_item",
        item={"kind": "assess", "issue": TICKET},
        reasons=["orphaned"],
    )
    assert merged["item"]["reasons"] == ["operator-items-closed", "orphaned"]
    everything = await board.call(
        AS_RECONCILER,
        "write_stage_item",
        item={"kind": "assess", "issue": {"repo": REPO, "number": 102}},
        reasons=[reason.value for reason in AssessReason],
    )
    assert len(everything["item"]["reasons"]) == 9
    with pytest.raises(ToolError):
        await board.call(
            AS_RECONCILER,
            "write_stage_item",
            item={"kind": "assess", "issue": {"repo": REPO, "number": 103}},
            reasons=["abandoned"],
        )
