"""A stage's run: its token over HTTP, its four tools, and the deploy-check result.

The board, the items and the claim helper are test_server.py's; the stub issuer and the
settings are test_auth.py's. Every token here is minted by a claim in the test.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import test_auth
import test_server
from fastmcp.exceptions import ToolError
from helpers import REPO, SHA_B
from key_value.aio.stores.memory import MemoryStore
from pydantic import ValidationError
from test_auth import ACCEPT, INIT, PRIVATE, ROUTE_HEADER, rpc_result
from test_server import (
    AS_OPERATOR,
    AS_RECONCILER,
    AS_SESSION,
    DECISION,
    MANUAL,
    RESULT,
    REVIEW,
    TICKET,
    WORK,
    Board,
    claim,
    lapse,
    staged,
)

from factory_board.auth import Identity, build_auth, identity_resolver
from factory_board.kinds import DeployCheckResult, ReviewResult
from factory_board.server import build_server
from factory_board.store import Caller, RunToken, Store, Tier

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from factory_board.config import ServerSettings

issuer = test_auth.issuer
settings = test_auth.settings
board = test_server.board

DEPLOY_CHECK: dict[str, Any] = {
    "kind": "deploy-check",
    "repo": REPO,
    "number": 202,
    "merge_sha": SHA_B,
    "issues": [TICKET],
}
RUN_ID = "20260101-deploy-check-01"
STUCK: dict[str, Any] = {
    "kind": "deploy-check",
    "outcome": "stuck",
    "summary": "Not synced a day after the merge.",
    "evidence": ["the widgets Application"],
}
NO_EVIDENCE: list[str] = []
RUN_DECISION = DECISION | {"summary": "The change is not live after a day."}


def as_run(run_id: str, item_id: str, stage: str) -> Identity:
    return Identity(
        Caller(Tier.AGENT, f"run:{run_id}"),
        operator_typed=False,
        run=RunToken(run_id, item_id, stage),
    )


async def running(
    board: Board, item: dict[str, Any] = DEPLOY_CHECK, run_id: str = RUN_ID
) -> tuple[Identity, str, str]:
    """A stage's item, claimed for a run: the run as a caller, the item's ID, the token."""
    item_id = await staged(board, item)
    claimed = await claim(board, item_id, run_id)
    return as_run(run_id, item_id, item["kind"]), item_id, claimed["token"]


def later(minutes: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


# ---- the result's fields ----


def test_a_deploy_check_result_names_what_was_live() -> None:
    parsed = DeployCheckResult.model_validate(RESULT)
    assert parsed.model_dump(mode="json") == RESULT | {"decision": None}
    by_image = RESULT | {
        "outcome": "unhealthy",
        "live": {"by": "image", "on": "widgets/press", "value": "ghcr.io/example/press:sha-abc"},
    }
    assert DeployCheckResult.model_validate(by_image).live is not None
    assert DeployCheckResult.model_validate(STUCK | {"decision": "abcdefgh"}).live is None


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({k: v for k, v in RESULT.items() if k != "live"}, "healthy needs `live`"),
        (RESULT | {"outcome": "unhealthy", "live": None}, "unhealthy needs `live`"),
        (RESULT | {"decision": "abcdefgh"}, "stuck change only"),
        (STUCK, "stuck needs `decision`"),
        (STUCK | {"decision": "abcdefgh", "live": RESULT["live"]}, "leave `live` out"),
        (STUCK | {"decision": "not-an-id"}, "decision"),
        (RESULT | {"live": {"by": "revision", "on": "widgets", "value": "main"}}, "full commit"),
        (RESULT | {"live": {"by": "tag", "on": "widgets", "value": SHA_B}}, "by"),
        (RESULT | {"live": {"by": "image", "on": "Widgets", "value": "a:b"}}, "on"),
        (RESULT | {"live": {"by": "image", "on": "widgets", "value": "a b"}}, "value"),
        (RESULT | {"outcome": "fine"}, "outcome"),
        (RESULT | {"evidence": NO_EVIDENCE}, "evidence"),
        (RESULT | {"evidence": ["a pod"] * 11}, "evidence"),
        (RESULT | {"evidence": ["two\nlines"]}, "evidence"),
        (RESULT | {"summary": ""}, "summary"),
        (RESULT | {"kind": "review"}, "kind"),
        (RESULT | {"verdict": "pass"}, "verdict"),
    ],
)
def test_a_result_that_does_not_fit_is_refused(result: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        DeployCheckResult.model_validate(result)


# ---- a review's result ----

PASSED: dict[str, Any] = {
    "kind": "review",
    "verdict": "pass",
    "block": None,
    "summary": "The diff does what the issue asks.",
    "checks": [
        {"check": "scope", "outcome": "pass", "note": None},
        {"check": "done-when", "outcome": "pass", "note": None},
        {"check": "test", "outcome": "not-applicable", "note": "Not a bug fix."},
        {"check": "quoted-data", "outcome": "pass", "note": None},
    ],
    "comment": 4_000_000_001,
    "decision": None,
}


def failing(check: str = "test", note: str | None = "No test fails without the fix.") -> list[Any]:
    return [
        c | {"outcome": "fail", "note": note} if c["check"] == check else c
        for c in PASSED["checks"]
    ]


FIXABLE = PASSED | {"verdict": "block", "block": "fixable", "checks": failing()}
ESCALATE = FIXABLE | {"block": "escalate"}


def test_a_review_result_has_the_verdict_and_every_check() -> None:
    assert ReviewResult.model_validate(PASSED).model_dump(mode="json") == PASSED
    bare = {k: v for k, v in PASSED.items() if k not in ("kind", "block", "decision")}
    assert ReviewResult.model_validate(bare).model_dump(mode="json") == PASSED
    assert ReviewResult.model_validate(FIXABLE).block == "fixable"
    assert ReviewResult.model_validate(ESCALATE | {"decision": "abcdefgh"}).decision == "abcdefgh"


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (PASSED | {"checks": failing()}, "a pass has no failed check: test"),
        (PASSED | {"block": "fixable"}, "for a block only"),
        (PASSED | {"decision": "abcdefgh"}, "for a block only"),
        (PASSED | {"verdict": "block"}, "a block needs `block`"),
        (FIXABLE | {"checks": PASSED["checks"]}, "a block needs a failed check"),
        (FIXABLE | {"checks": failing(note=None)}, "needs a `note`"),
        (FIXABLE | {"decision": "abcdefgh"}, "escalate block only"),
        (ESCALATE, "escalate needs `decision`"),
        (PASSED | {"checks": PASSED["checks"][:3]}, "checks"),
        (PASSED | {"checks": [*PASSED["checks"][:3], PASSED["checks"][0]]}, "every check once"),
        (PASSED | {"comment": 0}, "comment"),
        ({k: v for k, v in PASSED.items() if k != "comment"}, "comment"),
        (PASSED | {"verdict": "approve"}, "verdict"),
        (PASSED | {"summary": "two\nlines"}, "summary"),
        (PASSED | {"outcome": "healthy"}, "outcome"),
    ],
)
def test_a_review_result_that_does_not_fit_is_refused(result: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ReviewResult.model_validate(result)


async def test_a_review_run_closes_with_its_verdict(board: Board) -> None:
    reviewer, item_id, _ = await running(board, REVIEW, "20260101-review-01")
    # Another stage's result is not a review's.
    with pytest.raises(ToolError, match="a review run closes with a review result"):
        await board.call(reviewer, "close_my_item", result=RESULT)
    with pytest.raises(ToolError, match="a review run does not release its item"):
        await board.call(reviewer, "release_my_item", reason="not-live", not_before=later())
    closed = await board.call(reviewer, "close_my_item", result=PASSED)
    assert (closed["item"]["id"], closed["item"]["open"]) == (item_id, False)
    assert closed["item"]["result"] == PASSED
    # The reconciler reads the verdict and the commit it is on from the closed item.
    shown = await board.call(AS_RECONCILER, "get_item", item_id=item_id)
    assert (shown["fields"]["head_sha"], shown["result"]["verdict"]) == (REVIEW["head_sha"], "pass")


async def test_an_escalated_block_names_the_decision_the_run_wrote(board: Board) -> None:
    reviewer, _, _ = await running(board, REVIEW, "20260101-review-01")
    with pytest.raises(ToolError, match="a review run writes decision items, not a manual"):
        await board.call(reviewer, "create_item", item=MANUAL)
    theirs = await board.create(DECISION)
    with pytest.raises(ToolError, match="not a decision item this run wrote"):
        await board.call(reviewer, "close_my_item", result=ESCALATE | {"decision": theirs})
    mine = (await board.call(reviewer, "create_item", item=RUN_DECISION))["item"]["id"]
    closed = await board.call(reviewer, "close_my_item", result=ESCALATE | {"decision": mine})
    assert (closed["item"]["result"]["block"], closed["item"]["result"]["decision"]) == (
        "escalate",
        mine,
    )


async def test_a_deploy_check_run_does_not_close_with_a_review_result(board: Board) -> None:
    run, _, _ = await running(board)
    with pytest.raises(ToolError, match="a deploy-check run closes with a deploy-check result"):
        await board.call(run, "close_my_item", result=PASSED)


# ---- the run's tools ----


async def test_a_run_reads_the_item_it_holds(board: Board) -> None:
    run, item_id, token = await running(board)
    shown = await board.call(run, "my_item")
    assert (shown["id"], shown["kind"], shown["held_by"]) == (item_id, "deploy-check", run_name())
    assert shown["fields"]["merge_sha"] == SHA_B
    assert [(c["run"], c["seat"], c["seat_type"]) for c in shown["claims"]] == [
        (RUN_ID, "ottawa", "normal")
    ]
    assert shown["replies"] == []
    assert token not in json.dumps(shown)
    # It reads no other item, and nothing else of the board.
    other = await staged(board, REVIEW)
    for tool, arguments in (
        ("get_item", {"item_id": other}),
        ("list_items", {}),
        ("list_pipeline_items", {}),
    ):
        with pytest.raises(ToolError, match="not a tool of this caller's tier"):
            await board.call(run, tool, **arguments)


def run_name(run_id: str = RUN_ID) -> str:
    return f"run:{run_id}"


async def test_a_run_closes_its_item_with_the_result_and_its_token_dies(
    board: Board, store: Store
) -> None:
    run, item_id, token = await running(board)
    assert await store.run_for_token(token) == RunToken(RUN_ID, item_id, "deploy-check")
    closed = await board.call(run, "close_my_item", result=RESULT)
    assert closed["status"] == "closed"
    item = closed["item"]
    assert (item["open"], item["resolution"]) == (False, "done")
    assert item["result"] == RESULT | {"decision": None}
    assert await store.run_for_token(token) is None
    shown = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
    assert [(c["run"], c["ending"]) for c in shown["claims"]] == [(RUN_ID, "closed")]
    # The close of the last deploy-check that names the issue wakes assess, as any close.
    listed = await board.call(AS_RECONCILER, "list_pipeline_items")
    assert [(entry["kind"], entry["reasons"]) for entry in listed["items"]] == [
        ("assess", ["deploy-checks-closed"])
    ]
    # A closed item stays closed.
    with pytest.raises(ToolError, match="closed"):
        await board.call(run, "close_my_item", result=RESULT | {"outcome": "unhealthy"})


UNHEALTHY = RESULT | {"outcome": "unhealthy", "summary": "The widget press restarts in a loop."}


async def test_an_unhealthy_close_wakes_an_urgent_assess_item(board: Board) -> None:
    run, _, _ = await running(board)
    await board.call(run, "close_my_item", result=UNHEALTHY)
    listed = await board.call(AS_RECONCILER, "list_pipeline_items")
    assert [
        (entry["kind"], entry["subject"], entry["reasons"], entry["urgent"])
        for entry in listed["items"]
    ] == [("assess", f"issue {REPO}#101", ["deploy-checks-closed"], True)]
    queue = await board.call(test_server.AS_DISPATCHER, "list_claimable")
    assert [(entry["kind"], entry["rank"]) for entry in queue["items"]] == [("assess", 1)]


async def test_an_unhealthy_close_that_names_no_issue_wakes_assess_on_the_pr(
    board: Board,
) -> None:
    bare = {k: v for k, v in DEPLOY_CHECK.items() if k != "issues"}
    run, _, _ = await running(board, bare)
    await board.call(run, "close_my_item", result=UNHEALTHY)
    (woken,) = (await board.call(AS_RECONCILER, "list_pipeline_items"))["items"]
    assert (woken["kind"], woken["subject"], woken["reasons"], woken["urgent"]) == (
        "assess",
        f"pr {REPO}#202",
        ["deploy-checks-closed"],
        True,
    )
    assert woken["created_by"] == "process:board"
    assert woken["fields"] == {
        "kind": "assess",
        "issue": None,
        "pr": {"repo": REPO, "number": 202},
        "note": None,
    }
    # A healthy one wakes nothing.
    other = await running(board, bare | {"number": 203}, "20260101-deploy-check-02")
    await board.call(other[0], "close_my_item", result=RESULT)
    listed = (await board.call(AS_RECONCILER, "list_pipeline_items"))["items"]
    assert [entry["id"] for entry in listed] == [woken["id"]]


async def test_a_result_is_checked_before_anything_is_written(board: Board) -> None:
    run, item_id, _ = await running(board)
    for result in ({"outcome": "healthy"}, RESULT | {"live": None}, STUCK):
        with pytest.raises(ToolError):
            await board.call(run, "close_my_item", result=result)
    assert (await board.call(run, "my_item"))["open"]
    assert (await board.call(AS_OPERATOR, "get_item", item_id=item_id))["held_by"] == run_name()


async def test_stuck_closes_with_the_decision_the_run_wrote(board: Board) -> None:
    run, _, _ = await running(board)
    # Not a session's decision, not another kind, not an item that is not there.
    theirs = await board.create(DECISION)
    for decision in (theirs, "abcdefgh"):
        with pytest.raises(ToolError, match=r"not a decision item this run wrote|no item"):
            await board.call(run, "close_my_item", result=STUCK | {"decision": decision})
    written = await board.call(run, "create_item", item=RUN_DECISION)
    assert written["status"] == "created"
    mine = written["item"]
    assert (mine["kind"], mine["held_by"], mine["created_by"], mine["operator_typed"]) == (
        "decision",
        "human:operator",
        run_name(),
        False,
    )
    closed = await board.call(run, "close_my_item", result=STUCK | {"decision": mine["id"]})
    assert closed["item"]["result"]["decision"] == mine["id"]
    # The operator answers it like any decision.
    answered = await board.call(AS_OPERATOR, "answer", item_id=mine["id"], option=1)
    assert answered["status"].startswith("answered")


async def test_a_run_writes_only_the_kinds_its_stage_may(board: Board) -> None:
    run, _, _ = await running(board)
    with pytest.raises(ToolError, match="a deploy-check run writes decision items, not a manual"):
        await board.call(run, "create_item", item=MANUAL)
    worker, _, _ = await running(board, WORK, "20260101-work-01")
    with pytest.raises(ToolError, match="a work run writes no items, not a decision item"):
        await board.call(worker, "create_item", item=DECISION)
    assert (await board.call(AS_OPERATOR, "list_items"))["items"] == []
    # A session's create is as it was.
    assert (await board.call(AS_SESSION, "create_item", item=MANUAL))["status"] == "created"


async def test_a_deploy_check_that_is_not_live_is_released_until_later(
    board: Board, store: Store
) -> None:
    run, item_id, token = await running(board)
    for arguments, message in (
        ({"reason": "not-live"}, "needs not_before"),
        ({"reason": "not-live", "not_before": "2026-01-31T08:00:00"}, "UTC offset"),
        ({"reason": "stopped"}, "releases its item with not-live, not stopped"),
        ({"reason": "usage-limit", "not_before": later()}, "not usage-limit"),
        # A refused result is the dispatcher's to give: the runner releases with it.
        ({"reason": "result-refused"}, "not result-refused"),
        ({"reason": "not-live", "not_before": later(60 * 25)}, "at most a day ahead"),
    ):
        with pytest.raises(ToolError, match=message):
            await board.call(run, "release_my_item", **arguments)
    assert await store.run_for_token(token) is not None

    until = later()
    released = await board.call(run, "release_my_item", reason="not-live", not_before=until)
    item = released["item"]
    assert (released["status"], item["open"], item["held_by"]) == ("released", True, None)
    assert (item["releases"], item.get("expiries")) == (1, None)
    assert datetime.fromisoformat(item["deferred_until"]) == datetime.fromisoformat(until)
    assert await store.run_for_token(token) is None
    shown = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
    assert [(c["ending"], c["reason"]) for c in shown["claims"]] == [("released", "not-live")]
    # The run holds nothing any more.
    with pytest.raises(ToolError, match="held by nobody"):
        await board.call(run, "close_my_item", result=RESULT)


async def test_a_stage_without_result_fields_neither_closes_nor_releases(board: Board) -> None:
    worker, item_id, _ = await running(board, WORK, "20260101-work-01")
    with pytest.raises(ToolError, match="no result fields for a work run yet"):
        await board.call(worker, "close_my_item", result=RESULT)
    with pytest.raises(ToolError, match="a work run does not release its item"):
        await board.call(worker, "release_my_item", reason="not-live", not_before=later())
    shown = await board.call(worker, "my_item")
    assert (shown["id"], shown["open"], shown["held_by"]) == (
        item_id,
        True,
        run_name("20260101-work-01"),
    )


async def test_a_run_whose_claim_ran_out_closes_nothing(board: Board, conn: Any) -> None:
    run, item_id, _ = await running(board)
    lapse(conn, item_id)
    with pytest.raises(ToolError):
        await board.call(run, "close_my_item", result=RESULT)
    with pytest.raises(ToolError, match="run out"):
        await board.call(run, "release_my_item", reason="not-live", not_before=later())
    assert (await board.call(AS_OPERATOR, "get_item", item_id=item_id))["open"]


async def test_a_caller_named_like_a_run_without_a_run_s_token_has_no_item(board: Board) -> None:
    nameless = Identity(Caller(Tier.AGENT, run_name()), operator_typed=False)
    for tool, arguments in (
        ("my_item", {}),
        ("close_my_item", {"result": RESULT}),
        ("release_my_item", {"reason": "not-live", "not_before": later()}),
        ("create_item", {"item": DECISION}),
    ):
        with pytest.raises(ToolError, match="names no run"):
            await board.call(nameless, tool, **arguments)


# ---- the run's token over HTTP ----


@asynccontextmanager
async def served(settings: ServerSettings, store: Store) -> AsyncGenerator[httpx.AsyncClient]:
    """The real server with its auth, over ASGI. A context manager in the test's own task,
    as in test_auth.py."""
    auth, static = build_auth(settings, MemoryStore(), store.run_for_token)
    mcp = build_server(
        store, identity_resolver(settings.operator_group, static), settings.operator, auth=auth
    )
    asgi = mcp.http_app()
    async with (
        asgi.router.lifespan_context(asgi),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi), base_url=test_auth.BASE
        ) as client,
    ):
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def opened(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    """An MCP session with these headers; the headers for its calls."""
    response = await client.post("/mcp", json=INIT, headers=ACCEPT | headers)
    assert response.status_code == 200, response.text
    session = {"mcp-session-id": response.headers["mcp-session-id"]} | ACCEPT | headers
    await client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session
    )
    return session


async def tool(
    client: httpx.AsyncClient, session: dict[str, str], name: str, **arguments: Any
) -> httpx.Response:
    return await client.post(
        "/mcp",
        headers=session,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def answer(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    result = rpc_result(response)["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def test_a_run_s_token_signs_in_on_the_private_route_until_its_item_closes(
    board: Board, store: Store, settings: ServerSettings
) -> None:
    _, item_id, token = await running(board)
    async with served(settings, store) as client:
        session = await opened(client, PRIVATE | bearer(token))
        shown = answer(await tool(client, session, "my_item"))
        assert (shown["id"], shown["held_by"]) == (item_id, run_name())
        # It is a run and no session: a session's tools are refused, whatever it says it is.
        as_session = session | {test_auth.SESSION_HEADER: "0a1b2c3d"}
        refused = rpc_result(await tool(client, as_session, "list_items"))["result"]
        assert refused["isError"]
        assert "not a tool of this caller's tier" in refused["content"][0]["text"]

        closed = answer(await tool(client, session, "close_my_item", result=RESULT))
        assert (closed["status"], closed["item"]["resolution"]) == ("closed", "done")
        # The token is dead: for the open session and for a new one.
        assert (await tool(client, session, "my_item")).status_code == 401
        response = await client.post("/mcp", json=INIT, headers=ACCEPT | PRIVATE | bearer(token))
        assert response.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{}, {ROUTE_HEADER: "public"}, {ROUTE_HEADER: "Private"}, {"X-Other-Route": "private"}],
)
async def test_a_run_s_token_is_refused_off_the_private_route(
    board: Board, store: Store, settings: ServerSettings, headers: dict[str, str]
) -> None:
    _, _, token = await running(board)
    async with served(settings, store) as client:
        response = await client.post("/mcp", json=INIT, headers=ACCEPT | headers | bearer(token))
        assert response.status_code == 401
        # The same token on the private route is the run.
        await opened(client, PRIVATE | bearer(token))
    assert await store.run_for_token(token) is not None


async def test_only_a_running_claim_s_token_signs_in(
    board: Board, store: Store, settings: ServerSettings, conn: Any
) -> None:
    run, item_id, token = await running(board)
    hashed = test_auth.sha256(token)
    async with served(settings, store) as client:
        for wrong in (hashed, token + "x", "", "never-minted"):
            response = await client.post(
                "/mcp", json=INIT, headers=ACCEPT | PRIVATE | bearer(wrong)
            )
            assert response.status_code == 401, wrong
        session = await opened(client, PRIVATE | bearer(token))
        # A claim that ran out is dead before anyone counts it.
        lapse(conn, item_id)
        assert (await tool(client, session, "my_item")).status_code == 401
    # And a released one.
    other, _, second = await running(
        board, DEPLOY_CHECK | {"number": 203}, "20260101-deploy-check-02"
    )
    async with served(settings, store) as client:
        session = await opened(client, PRIVATE | bearer(second))
        released = answer(
            await tool(client, session, "release_my_item", reason="not-live", not_before=later())
        )
        assert released["status"] == "released"
        assert (await tool(client, session, "my_item")).status_code == 401
    assert run != other


async def test_the_runs_tokens_are_off_without_the_store_s_lookup(
    board: Board, store: Store, settings: ServerSettings
) -> None:
    _, _, token = await running(board)
    auth, static = build_auth(settings, MemoryStore())
    assert static is not None
    assert await static.verify_token(token) is None
    mcp = build_server(
        store, identity_resolver(settings.operator_group, static), "olive", auth=auth
    )
    asgi = mcp.http_app()
    async with (
        asgi.router.lifespan_context(asgi),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi), base_url=test_auth.BASE
        ) as client,
    ):
        response = await client.post("/mcp", json=INIT, headers=ACCEPT | PRIVATE | bearer(token))
        assert response.status_code == 401
