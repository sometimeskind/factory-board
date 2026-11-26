"""The pause flag: the operator sets and lifts it, the reconciler sets it too, every tier
reads it, the dispatcher gets it with the queue, a claim is refused under it, and the
metrics export it. Every name and reason here is invented.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString

import psycopg
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from helpers import DISPATCHER, NIGHTLY, OPERATOR, RECONCILER, RUN, SESSION, as_tier, pr, stage
from prometheus_client import REGISTRY
from psycopg import errors

from factory_board import metrics
from factory_board.auth import Identity
from factory_board.server import build_server
from factory_board.store import Caller, NotPermitted, Paused, Resolution, Store, Tier

AS_OPERATOR = Identity(OPERATOR, operator_typed=True)
AS_SESSION = Identity(SESSION, operator_typed=True)
AS_NIGHTLY = Identity(NIGHTLY, operator_typed=False)
AS_RECONCILER = Identity(RECONCILER, operator_typed=False)
AS_DISPATCHER = Identity(DISPATCHER, operator_typed=False)
METRICS = Caller(Tier.PROCESS, "process:metrics")
OTHERS = [SESSION, RUN, NIGHTLY, RECONCILER, DISPATCHER]
# Who may not pause: everyone but the operator and the reconciler.
NO_PAUSE = [SESSION, RUN, NIGHTLY, DISPATCHER]
WHY = "the widget press is being moved"
SET: LiteralString = "SELECT * FROM set_pause(%s)"
LIFT: LiteralString = "SELECT * FROM lift_pause()"


class Board:
    """The server with a caller the test switches."""

    def __init__(self, store: Store) -> None:
        self.identity = AS_SESSION
        self.server = build_server(store, lambda: self.identity, "operator")

    async def call(self, identity: Identity, tool: str, **arguments: Any) -> dict[str, Any]:
        self.identity = identity
        async with Client(self.server) as client:
            return (await client.call_tool(tool, arguments)).data


@pytest.fixture
def board(store: Store) -> Board:
    return Board(store)


@pytest.fixture
def owner(migrated: None, owner_url: str) -> Any:
    with psycopg.connect(owner_url, autocommit=True) as connection:
        yield connection


# ---- the rules, with plain SQL past the store ----


def test_the_operator_pauses_and_a_second_pause_keeps_the_first(
    app_url: str, owner: psycopg.Connection
) -> None:
    with as_tier(app_url, OPERATOR) as conn:
        first = conn.execute(SET, (WHY,)).fetchone()
    assert first is not None
    assert first[1:] == (OPERATOR.identity, WHY, True)
    with as_tier(app_url, OPERATOR) as conn:
        second = conn.execute(SET, ("something else",)).fetchone()
    assert second == (first[0], OPERATOR.identity, WHY, False)
    assert owner.execute("SELECT only_row, paused_at, paused_by, reason FROM pause").fetchall() == [
        (True, first[0], OPERATOR.identity, WHY)
    ]


def test_the_operator_lifts_the_pause_and_lifting_none_returns_no_row(
    app_url: str, owner: psycopg.Connection
) -> None:
    with as_tier(app_url, OPERATOR) as conn:
        assert conn.execute(LIFT).fetchall() == []
        paused = conn.execute(SET, (WHY,)).fetchone()
    assert paused is not None
    with as_tier(app_url, OPERATOR) as conn:
        assert conn.execute(LIFT).fetchall() == [(paused[0], OPERATOR.identity, WHY)]
    assert owner.execute("SELECT count(*) FROM pause").fetchone() == (0,)


@pytest.mark.parametrize("caller", OTHERS, ids=lambda c: c.identity)
def test_only_the_operator_lifts_and_only_the_reconciler_pauses_besides(
    app_url: str, owner: psycopg.Connection, caller: Caller
) -> None:
    if caller in NO_PAUSE:
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(SET, (WHY,))
        assert owner.execute("SELECT count(*) FROM pause").fetchone() == (0,)
    with as_tier(app_url, OPERATOR) as conn:
        conn.execute(SET, (WHY,))
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        conn.execute(LIFT)
    assert owner.execute("SELECT count(*) FROM pause").fetchone() == (1,)


@pytest.mark.parametrize("caller", [OPERATOR, *OTHERS], ids=lambda c: c.identity)
def test_every_tier_reads_the_pause_and_none_writes_the_table(
    app_url: str, owner: psycopg.Connection, caller: Caller
) -> None:
    owner.execute(
        "INSERT INTO pause (paused_at, paused_by, reason) VALUES (now(), 'human:operator', %s)",
        (WHY,),
    )
    with as_tier(app_url, caller) as conn:
        assert conn.execute("SELECT paused_by, reason FROM pause").fetchall() == [
            ("human:operator", WHY)
        ]
    writes: list[LiteralString] = [
        "INSERT INTO pause (paused_at, paused_by, reason) VALUES (now(), 'human:operator', 'x')",
        "UPDATE pause SET reason = 'x'",
        "DELETE FROM pause",
    ]
    for statement in writes:
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(statement)


@pytest.mark.parametrize("reason", ["", "x" * 501])
def test_a_reason_is_one_to_five_hundred_characters(
    migrated: None, app_url: str, reason: str
) -> None:
    with pytest.raises(errors.CheckViolation), as_tier(app_url, OPERATOR) as conn:
        conn.execute(SET, (reason,))


def test_the_table_holds_one_row_at_most(owner: psycopg.Connection) -> None:
    insert: LiteralString = (
        "INSERT INTO pause (only_row, paused_at, paused_by, reason) "
        "VALUES (%s, now(), 'human:operator', 'x')"
    )
    with pytest.raises(errors.CheckViolation):
        owner.execute(insert, (False,))
    owner.execute(insert, (True,))
    with pytest.raises(errors.UniqueViolation):
        owner.execute(insert, (True,))


def test_the_reconciler_pauses_under_its_own_name_and_no_other_process_does(
    app_url: str, owner: psycopg.Connection
) -> None:
    with as_tier(app_url, RECONCILER) as conn:
        row = conn.execute(SET, (WHY,)).fetchone()
    assert row is not None and (row[1], row[2], row[3]) == (RECONCILER.identity, WHY, True)
    # The operator's pause after it keeps the reconciler's, and the operator lifts it.
    with as_tier(app_url, OPERATOR) as conn:
        row = conn.execute(SET, ("something else",)).fetchone()
        assert row is not None and (row[1], row[3]) == (RECONCILER.identity, False)
        assert conn.execute(LIFT).fetchone() is not None
    # The table takes a person's name or the reconciler's, whatever the function checks.
    for name in ("process:dispatcher", "process:reconciler2", "run:20260101-work-01"):
        with pytest.raises(errors.CheckViolation, match="pause_paused_by_format"):
            owner.execute(
                "INSERT INTO pause (paused_at, paused_by, reason) VALUES (now(), %s, %s)",
                (name, WHY),
            )


def test_the_two_functions_are_the_owner_s_and_only_the_reconciler_s_tier_pauses_besides(
    owner: psycopg.Connection,
) -> None:
    rows = owner.execute(
        "SELECT proname, prosecdef, proconfig, pg_get_userbyid(proowner), proacl::text "
        "FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
        "AND proname IN ('set_pause', 'lift_pause') ORDER BY proname"
    ).fetchall()
    path = ["search_path=pg_catalog, pg_temp"]
    acl = "{board_owner=X/board_owner,board_operator=X/board_owner"
    assert rows == [
        ("lift_pause", True, path, "board_owner", acl + "}"),
        ("set_pause", True, path, "board_owner", acl + ",board_process=X/board_owner}"),
    ]


# ---- through the store and the tools ----


async def test_the_operator_pauses_and_resumes_with_the_tools(board: Board, store: Store) -> None:
    assert await store.pause_state(DISPATCHER) is None
    assert await board.call(AS_OPERATOR, "resume") == {"status": "not-paused"}

    before = datetime.now(UTC) - timedelta(seconds=5)
    paused = await board.call(AS_OPERATOR, "pause", reason=WHY)
    assert paused["status"] == "paused"
    standing = paused["paused"]
    assert (standing["by"], standing["reason"]) == (OPERATOR.identity, WHY)
    assert before < datetime.fromisoformat(standing["since"]) < before + timedelta(seconds=10)

    again = await board.call(AS_OPERATOR, "pause", reason="something else")
    assert again == {"status": "already-paused", "paused": standing}

    for caller in (OPERATOR, SESSION, NIGHTLY, RECONCILER, DISPATCHER):
        state = await store.pause_state(caller)
        assert state is not None and state.reason == WHY

    assert await board.call(AS_OPERATOR, "resume") == {"status": "resumed", "was": standing}
    assert await store.pause_state(DISPATCHER) is None


async def test_nobody_else_has_the_two_tools(board: Board, store: Store) -> None:
    for identity in (AS_SESSION, AS_NIGHTLY, AS_DISPATCHER):
        with pytest.raises(ToolError, match="not a tool of this caller's tier"):
            await board.call(identity, "pause", reason=WHY)
    for identity in (AS_SESSION, AS_NIGHTLY, AS_RECONCILER, AS_DISPATCHER):
        with pytest.raises(ToolError, match="not a tool of this caller's tier"):
            await board.call(identity, "resume")
    for caller in NO_PAUSE:
        with pytest.raises(NotPermitted):
            await store.set_pause(caller, WHY)
    for caller in OTHERS:
        with pytest.raises(NotPermitted):
            await store.lift_pause(caller)
    assert await store.pause_state(OPERATOR) is None


async def test_the_reconciler_pauses_with_the_tool_and_the_operator_resumes(
    board: Board, store: Store
) -> None:
    paused = await board.call(AS_RECONCILER, "pause", reason=WHY)
    assert paused["status"] == "paused"
    assert paused["paused"]["by"] == RECONCILER.identity
    with pytest.raises(NotPermitted):
        await store.lift_pause(RECONCILER)
    resumed = await board.call(AS_OPERATOR, "resume")
    assert resumed == {"status": "resumed", "was": paused["paused"]}


async def test_a_claim_is_refused_while_paused_but_for_health_and_digest(
    board: Board, store: Store
) -> None:
    review = (await store.create_item(RECONCILER, replace(stage("review"), subject=pr(202)))).item
    urgent = replace(stage("deploy-check"), subject=pr(203), urgent=True)
    bumped = (await store.create_item(RECONCILER, urgent)).item
    health = (await store.create_item(RECONCILER, stage("health"))).item
    digest = (await store.create_item(RECONCILER, stage("digest"))).item
    await store.set_pause(RECONCILER, WHY)

    # Urgent or not, and the item is left as it was.
    for item, run, seat_type in (
        (review, "20260101-review-01", "normal"),
        (bumped, "20260101-deploy-check-01", "urgent"),
    ):
        with pytest.raises(Paused, match="the factory is paused"):
            await store.claim_item(DISPATCHER, item.id, run, 30, seat="tokyo", seat_type=seat_type)
        assert await store.get_item(OPERATOR, item.id) == item
    with pytest.raises(ToolError, match="the factory is paused"):
        await board.call(
            AS_DISPATCHER,
            "claim",
            item_id=review.id,
            run_id="20260101-review-01",
            cap_minutes=30,
            seat="tokyo",
            seat_type="normal",
        )
    assert await store.list_claims(OPERATOR, review.id) == []

    await store.claim_item(
        DISPATCHER, health.id, "20260101-health-01", 30, seat="lisbon", seat_type="normal"
    )
    await store.claim_item(
        DISPATCHER, digest.id, "20260101-digest-01", 30, seat="athens", seat_type="normal"
    )
    # A run that is going is not stopped: it closes its item under the pause.
    health_run = Caller(Tier.AGENT, "run:20260101-health-01")
    await store.close_item(health_run, health.id, Resolution.DONE, {"findings": []})

    await store.lift_pause(OPERATOR)
    # The refused run never claimed, so its ID is still free.
    claimed = await store.claim_item(
        DISPATCHER, review.id, "20260101-review-01", 30, seat="tokyo", seat_type="normal"
    )
    assert not claimed.stale


@pytest.mark.parametrize("reason", ["", "x" * 501])
async def test_the_tool_refuses_a_reason_out_of_bounds(board: Board, reason: str) -> None:
    with pytest.raises(ToolError):
        await board.call(AS_OPERATOR, "pause", reason=reason)


async def test_the_dispatcher_reads_the_pause_with_the_queue(board: Board) -> None:
    assert (await board.call(AS_DISPATCHER, "list_claimable"))["paused"] is None
    standing = (await board.call(AS_OPERATOR, "pause", reason=WHY))["paused"]
    assert (await board.call(AS_DISPATCHER, "list_claimable"))["paused"] == standing
    await board.call(AS_OPERATOR, "resume")
    assert (await board.call(AS_DISPATCHER, "list_claimable"))["paused"] is None


async def test_the_metrics_export_the_pause(board: Board, store: Store) -> None:
    def exported() -> float | None:
        return REGISTRY.get_sample_value("factory_board_paused")

    assert (await store.stats(METRICS)).paused is False
    metrics.show(await store.stats(METRICS))
    assert exported() == 0
    await board.call(AS_OPERATOR, "pause", reason=WHY)
    metrics.show(await store.stats(METRICS))
    assert exported() == 1
    await board.call(AS_OPERATOR, "resume")
    metrics.show(await store.stats(METRICS))
    assert exported() == 0
