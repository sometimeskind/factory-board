"""The pause flag: the operator sets and lifts it, every tier reads it, the dispatcher gets
it with the queue, and the metrics export it. Every name and reason here is invented.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString

import psycopg
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from helpers import DISPATCHER, NIGHTLY, OPERATOR, RECONCILER, RUN, SESSION, as_tier
from prometheus_client import REGISTRY
from psycopg import errors

from factory_board import metrics
from factory_board.auth import Identity
from factory_board.server import build_server
from factory_board.store import Caller, NotPermitted, Store, Tier

AS_OPERATOR = Identity(OPERATOR, operator_typed=True)
AS_SESSION = Identity(SESSION, operator_typed=True)
AS_NIGHTLY = Identity(NIGHTLY, operator_typed=False)
AS_RECONCILER = Identity(RECONCILER, operator_typed=False)
AS_DISPATCHER = Identity(DISPATCHER, operator_typed=False)
METRICS = Caller(Tier.PROCESS, "process:metrics")
OTHERS = [SESSION, RUN, NIGHTLY, RECONCILER, DISPATCHER]
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
def test_only_the_operator_pauses_and_lifts(
    app_url: str, owner: psycopg.Connection, caller: Caller
) -> None:
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


def test_the_two_functions_are_the_owner_s_and_only_the_operator_s_tier_calls_them(
    owner: psycopg.Connection,
) -> None:
    rows = owner.execute(
        "SELECT proname, prosecdef, proconfig, pg_get_userbyid(proowner), proacl::text "
        "FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
        "AND proname IN ('set_pause', 'lift_pause') ORDER BY proname"
    ).fetchall()
    path = ["search_path=pg_catalog, pg_temp"]
    acl = "{board_owner=X/board_owner,board_operator=X/board_owner}"
    assert rows == [
        ("lift_pause", True, path, "board_owner", acl),
        ("set_pause", True, path, "board_owner", acl),
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
    for identity in (AS_SESSION, AS_NIGHTLY, AS_RECONCILER, AS_DISPATCHER):
        with pytest.raises(ToolError, match="not a tool of this caller's tier"):
            await board.call(identity, "pause", reason=WHY)
        with pytest.raises(ToolError, match="not a tool of this caller's tier"):
            await board.call(identity, "resume")
    for caller in OTHERS:
        with pytest.raises(NotPermitted):
            await store.set_pause(caller, WHY)
        with pytest.raises(NotPermitted):
            await store.lift_pause(caller)
    assert await store.pause_state(OPERATOR) is None


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
