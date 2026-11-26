"""The tool surface over a real store: who may call what, and what each verb does.

Every repo, number, name and command here is invented.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

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
    seat_of,
)
from prometheus_client import REGISTRY

from factory_board import metrics
from factory_board.auth import Identity
from factory_board.server import build_server, who
from factory_board.store import Caller, Store, Tier

AS_OPERATOR = Identity(OPERATOR, operator_typed=True)
AS_SESSION = Identity(SESSION, operator_typed=True)
AS_NIGHTLY = Identity(NIGHTLY, operator_typed=False)
AS_RECONCILER = Identity(RECONCILER, operator_typed=False)
AS_DISPATCHER = Identity(DISPATCHER, operator_typed=False)

TICKET = {"repo": REPO, "number": 101}
MANUAL: dict[str, Any] = {
    "kind": "manual",
    "ticket": TICKET,
    "summary": "Restock the widget press.",
    "command": "make widgets",
    "machine": "workstation",
}
DECISION: dict[str, Any] = {
    "kind": "decision",
    "ticket": TICKET,
    "summary": "The press runs hot.",
    "options": ["cool it", "leave it"],
}
PR_APPROVE: dict[str, Any] = {
    "kind": "pr-approve",
    "summary": "Touches the widget press.",
    "repo": REPO,
    "number": 202,
    "head_sha": SHA_A,
    "machine": "browser",
}
KEYED: dict[str, Any] = {
    "kind": "manual",
    "summary": "The press is hot.",
    "command": "open the vent",
    "machine": "pilot",
}
KEY = "finding:widget-press-hot"

ASSESS: dict[str, Any] = {"kind": "assess", "issue": TICKET}
REVIEW: dict[str, Any] = {"kind": "review", "repo": REPO, "number": 202, "head_sha": SHA_A}
DEPLOY_CHECK: dict[str, Any] = {
    "kind": "deploy-check",
    "repo": REPO,
    "number": 202,
    "merge_sha": SHA_B,
}

READS = {"get_item", "list_pipeline_items", "list_assessments"}
TOOLS = {
    "operator": {
        "list_items",
        "done",
        "answer",
        "reply",
        "defer",
        "reject",
        "bump",
        "pause",
        "resume",
    }
    | READS,
    "session": {"create_item", "list_items", "update_pr_head"} | READS,
    "nightly": {"create_keyed_item", "list_own_items", "close_own_item"},
    "reconciler": {
        "list_items",
        "write_stage_item",
        "write_operator_item",
        "close_for_fact",
        "heartbeat",
        "pause",
    }
    | READS,
    "dispatcher": {"list_claimable", "claim", "release"} | READS,
}
AHEAD = (datetime.now(UTC) + timedelta(days=2)).isoformat()
# One call per tool that passes validation; the ID names no item.
CALLS: dict[str, dict[str, Any]] = {
    "list_items": {},
    "get_item": {"item_id": "abcdefgh"},
    "done": {"item_id": "abcdefgh"},
    "answer": {"item_id": "abcdefgh", "option": 1},
    "reply": {"item_id": "abcdefgh", "text": "ok"},
    "defer": {"item_id": "abcdefgh", "until": AHEAD},
    "reject": {"item_id": "abcdefgh", "reason": "no"},
    "bump": {"item_id": "abcdefgh"},
    "create_item": {"item": MANUAL},
    "update_pr_head": {"item_id": "abcdefgh", "repo": REPO, "number": 202, "head_sha": SHA_B},
    "create_keyed_item": {"key": KEY, "item": KEYED},
    "list_own_items": {},
    "close_own_item": {"item_id": "abcdefgh"},
    "list_pipeline_items": {},
    "list_assessments": {"repo": REPO},
    "write_stage_item": {"item": ASSESS},
    "write_operator_item": {"item": PR_APPROVE},
    "close_for_fact": {"item_id": "abcdefgh", "cause": "merged"},
    "heartbeat": {},
    "list_claimable": {},
    "claim": {
        "item_id": "abcdefgh",
        "run_id": "20260101-review-01",
        "cap_minutes": 30,
        "seat": "review/1",
    },
    "release": {"item_id": "abcdefgh", "reason": "stopped"},
    "pause": {"reason": "the widget press is being moved"},
    "resume": {},
}


class Board:
    """The server with a caller the test switches."""

    def __init__(self, store: Store) -> None:
        self.identity = AS_SESSION
        self.server = build_server(store, lambda: self.identity, "operator")

    async def call(self, identity: Identity, tool: str, **arguments: Any) -> dict[str, Any]:
        self.identity = identity
        async with Client(self.server) as client:
            return (await client.call_tool(tool, arguments)).data

    async def create(self, item: dict[str, Any], **arguments: Any) -> str:
        created = await self.call(AS_SESSION, "create_item", item=item, **arguments)
        assert created["status"] == "created", created
        return created["item"]["id"]

    async def keyed(self) -> str:
        created = await self.call(AS_NIGHTLY, "create_keyed_item", key=KEY, item=KEYED)
        return created["item"]["id"]


@pytest.fixture
def board(store: Store) -> Board:
    return Board(store)


async def test_the_tools_are_the_three_rows(board: Board) -> None:
    async with Client(board.server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == set().union(*TOOLS.values()) == set(CALLS)


@pytest.mark.parametrize(
    "identity", [AS_OPERATOR, AS_SESSION, AS_NIGHTLY, AS_RECONCILER, AS_DISPATCHER]
)
async def test_a_tool_outside_the_tier_s_row_is_refused(
    board: Board, store: Store, identity: Identity
) -> None:
    for tool in sorted(set(CALLS) - TOOLS[who(identity)]):
        with pytest.raises(ToolError, match="not a tool of this caller's tier"):
            await board.call(identity, tool, **CALLS[tool])
    assert await store.list_open(OPERATOR, include_deferred=True) == []


async def test_the_reconciler_lists_the_operator_s_items(board: Board) -> None:
    item = (await board.call(AS_SESSION, "create_item", item=MANUAL))["item"]
    listed = (await board.call(AS_RECONCILER, "list_items", include_deferred=True))["items"]
    assert [entry["id"] for entry in listed] == [item["id"]]


async def test_an_item_done_elsewhere_shows_its_place(board: Board) -> None:
    elsewhere = MANUAL | {"machine": "other", "place": "widget press"}
    item = (await board.call(AS_SESSION, "create_item", item=elsewhere))["item"]
    assert (item["machine"], item["place"]) == ("other", "widget press")
    plain = (await board.call(AS_SESSION, "create_item", item=MANUAL))["item"]
    assert "place" not in plain


async def test_a_session_s_item_is_held_by_the_operator_and_rendered(board: Board) -> None:
    created = await board.call(AS_SESSION, "create_item", item=MANUAL)
    item = created["item"]
    assert created["confirmation"] == (f"manual for issue {REPO}#101: free text: make widgets")
    assert item["held_by"] == "human:operator"
    assert item["created_by"] == SESSION.identity
    assert item["operator_typed"] is True
    assert (item["machine"], item["commands"], item["free_text"]) == (
        "workstation",
        ["make widgets"],
        True,
    )
    listed = await board.call(AS_OPERATOR, "list_items")
    assert [entry["id"] for entry in listed["items"]] == [item["id"]]


async def test_an_item_needs_a_ticket_or_a_key(board: Board) -> None:
    with pytest.raises(ToolError, match="needs a ticket or a key"):
        await board.call(AS_SESSION, "create_item", item=KEYED)
    created = await board.call(AS_SESSION, "create_item", item=KEYED, key=KEY)
    assert created["item"]["subject"] == f"finding {KEY}"


@pytest.mark.parametrize(
    "change",
    [
        {"created_by": "human:operator"},
        {"claimed_by": SESSION.identity},
        {"operator_typed": False},
        {"urgent": True},
        {"command": "make widgets\nrm -rf /"},
        {"waiting_on": []},
    ],
)
async def test_a_create_takes_nothing_about_the_caller_from_its_input(
    board: Board, change: dict[str, Any]
) -> None:
    with pytest.raises(ToolError):
        await board.call(AS_SESSION, "create_item", item=MANUAL | change)
    with pytest.raises(ToolError):
        await board.call(AS_SESSION, "create_item", item=MANUAL, **change)


async def test_a_pr_item_is_about_its_own_pr_and_its_head_can_move(board: Board) -> None:
    item_id = await board.create(PR_APPROVE | {"ticket": TICKET})
    item = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
    assert item["subject"] == f"pr {REPO}#202"
    assert "head_sha_moves" not in item

    again = await board.call(AS_SESSION, "create_item", item=PR_APPROVE)
    assert (again["status"], again["item"]["id"]) == ("already open", item_id)

    with pytest.raises(ToolError, match="not a pr-approve or pr-merge item"):
        await board.call(
            AS_SESSION, "update_pr_head", item_id=item_id, repo=REPO, number=203, head_sha=SHA_B
        )
    moved = await board.call(
        AS_SESSION, "update_pr_head", item_id=item_id, repo=REPO, number=202, head_sha=SHA_B
    )
    assert SHA_B[:12] in moved["confirmation"]
    item = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
    assert [(move["from"], move["to"], move["by"]) for move in item["head_sha_moves"]] == [
        (SHA_A, SHA_B, SESSION.identity)
    ]


async def test_done_closes_a_held_item_and_nothing_else(board: Board) -> None:
    item_id = await board.create(MANUAL)
    done = await board.call(AS_OPERATOR, "done", item_id=item_id)
    assert done["confirmation"].endswith("free text: make widgets")
    closed = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
    assert (closed["open"], closed["resolution"]) == (False, "done")
    with pytest.raises(ToolError, match="is closed"):
        await board.call(AS_OPERATOR, "done", item_id=item_id)

    with pytest.raises(ToolError, match="answer, not done"):
        await board.call(AS_OPERATOR, "done", item_id=await board.create(DECISION))
    with pytest.raises(ToolError, match="held by process:nightly"):
        await board.call(AS_OPERATOR, "done", item_id=await board.keyed())
    with pytest.raises(ToolError, match="no item"):
        await board.call(AS_OPERATOR, "done", item_id="abcdefgh")


async def test_answer_takes_an_option_or_a_text(board: Board) -> None:
    item_id = await board.create(DECISION)
    for bad in ({}, {"option": 1, "text": "both"}, {"option": 3}):
        with pytest.raises(ToolError):
            await board.call(AS_OPERATOR, "answer", item_id=item_id, **bad)
    answered = await board.call(AS_OPERATOR, "answer", item_id=item_id, option=2)
    assert answered["confirmation"].endswith(": leave it")
    closed = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
    assert closed["resolution"] == "answered"
    assert closed["result"] == {"option": 2, "answer": "leave it"}

    other = await board.create(DECISION | {"ticket": {"repo": REPO, "number": 102}})
    await board.call(AS_OPERATOR, "answer", item_id=other, text="sell the press")
    closed = await board.call(AS_OPERATOR, "get_item", item_id=other)
    assert closed["result"] == {"answer": "sell the press"}

    with pytest.raises(ToolError, match="not a decision"):
        await board.call(AS_OPERATOR, "answer", item_id=await board.create(MANUAL), option=1)


async def test_reply_closes_a_held_item_and_only_attaches_to_another(board: Board) -> None:
    held = await board.create(MANUAL)
    replied = await board.call(AS_OPERATOR, "reply", item_id=held, text="did it by hand")
    assert replied["status"] == "replied and closed"
    closed = await board.call(AS_OPERATOR, "get_item", item_id=held)
    assert closed["resolution"] == "replied"
    assert [entry["text"] for entry in closed["replies"]] == ["did it by hand"]

    unheld = await board.keyed()
    replied = await board.call(AS_OPERATOR, "reply", item_id=unheld, text="vent is stuck")
    assert "stays open with process:nightly" in replied["status"]
    assert "nothing is woken" in replied["note"]
    own = await board.call(AS_NIGHTLY, "list_own_items")
    assert [(entry["id"], entry["open"]) for entry in own["items"]] == [(unheld, True)]
    assert own["items"][0]["replies"][0]["text"] == "vent is stuck"
    assert own["items"][0]["replies"][0]["author"] == OPERATOR.identity


async def test_reject_and_bump_work_on_an_item_the_operator_does_not_hold(board: Board) -> None:
    unheld = await board.keyed()
    bumped = await board.call(AS_OPERATOR, "bump", item_id=unheld)
    assert bumped["status"] == "urgent"
    assert (await board.call(AS_OPERATOR, "get_item", item_id=unheld))["urgent"] is True

    await board.call(AS_OPERATOR, "reject", item_id=unheld, reason="the vent is fine")
    closed = await board.call(AS_OPERATOR, "get_item", item_id=unheld)
    assert (closed["resolution"], closed["result"]) == ("rejected", {"reason": "the vent is fine"})
    assert (await board.call(AS_NIGHTLY, "list_own_items"))["items"] == []
    # The finding can be written again after the reject.
    assert await board.keyed() != unheld


async def test_defer_hides_a_held_item_until_a_time_ahead(board: Board) -> None:
    item_id = await board.create(MANUAL)
    for bad in ("2020-01-01T00:00:00+00:00", "2999-01-01T00:00:00"):
        with pytest.raises(ToolError):
            await board.call(AS_OPERATOR, "defer", item_id=item_id, until=bad)
    with pytest.raises(ToolError, match="held by process:nightly"):
        await board.call(AS_OPERATOR, "defer", item_id=await board.keyed(), until=AHEAD)

    await board.call(AS_OPERATOR, "defer", item_id=item_id, until=AHEAD)
    listed = await board.call(AS_OPERATOR, "list_items")
    assert item_id not in [entry["id"] for entry in listed["items"]]
    listed = await board.call(AS_OPERATOR, "list_items", include_deferred=True)
    assert item_id in [entry["id"] for entry in listed["items"]]


async def test_the_nightly_closes_only_what_it_holds(board: Board) -> None:
    own = await board.keyed()
    again = await board.call(AS_NIGHTLY, "create_keyed_item", key=KEY, item=KEYED)
    assert (again["status"], again["item"]["id"]) == ("already open", own)
    assert again["item"]["held_by"] == NIGHTLY.identity
    assert again["item"]["operator_typed"] is False

    others = await board.create(MANUAL)
    with pytest.raises(ToolError, match="held by human:operator"):
        await board.call(AS_NIGHTLY, "close_own_item", item_id=others)

    await board.call(AS_NIGHTLY, "close_own_item", item_id=own)
    closed = await board.call(AS_OPERATOR, "get_item", item_id=own)
    assert closed["resolution"] == "superseded"


def numbered(item: dict[str, Any], number: int) -> dict[str, Any]:
    """The same item on another made-up issue."""
    return item | {"ticket": {"repo": REPO, "number": number}}


async def test_an_item_shows_what_it_waits_on_and_only_then(board: Board) -> None:
    first = await board.create(MANUAL)
    plain = await board.call(AS_OPERATOR, "get_item", item_id=first)
    assert not {"after", "waiting_on", "holds"} & set(plain)

    created = await board.call(
        AS_SESSION, "create_item", item=numbered(DECISION, 102), after=[first], holds=False
    )
    second = created["item"]
    assert (second["after"], second["waiting_on"], second["holds"]) == ([first], [first], False)
    # Not hidden from the list: after what can be done now.
    third = await board.create(numbered(MANUAL, 103))
    listed = await board.call(AS_OPERATOR, "list_items")
    assert [entry["id"] for entry in listed["items"]] == [first, third, second["id"]]
    assert listed["items"][2]["waiting_on"] == [first]

    await board.call(AS_OPERATOR, "done", item_id=first)
    released = await board.call(AS_OPERATOR, "get_item", item_id=second["id"])
    assert released["after"] == [first]
    assert "waiting_on" not in released
    assert released["holds"] is False


async def test_the_nightly_s_item_can_wait_too(board: Board) -> None:
    first = await board.create(MANUAL)
    created = await board.call(AS_NIGHTLY, "create_keyed_item", key=KEY, item=KEYED, after=[first])
    assert created["item"]["waiting_on"] == [first]
    # Whether an item holds its issue is said with a ticket, and the nightly's has none.
    with pytest.raises(ToolError):
        await board.call(AS_NIGHTLY, "create_keyed_item", key=KEY, item=KEYED, holds=False)


async def test_holds_false_needs_an_issue(board: Board) -> None:
    for item, arguments in [
        (KEYED, {"key": KEY}),
        (PR_APPROVE, {}),
        (PR_APPROVE | {"ticket": TICKET}, {}),
    ]:
        with pytest.raises(ToolError, match="holds=false is for an item on an issue"):
            await board.call(AS_SESSION, "create_item", item=item, holds=False, **arguments)
    assert (await board.call(AS_OPERATOR, "list_items"))["items"] == []


async def test_what_an_item_is_after_is_checked(board: Board) -> None:
    first = await board.create(MANUAL)
    rejected = await board.create(numbered(MANUAL, 102))
    await board.call(AS_OPERATOR, "reject", item_id=rejected, reason="no")
    for bad, message in [
        (["abcdefgh"], "no item abcdefgh"),
        ([first, first], "twice"),
        ([rejected], "was closed as rejected"),
    ]:
        with pytest.raises(ToolError, match=message):
            await board.call(AS_SESSION, "create_item", item=numbered(MANUAL, 103), after=bad)
    for bad in (["not an id"], [first] * 11, first):
        with pytest.raises(ToolError):
            await board.call(AS_SESSION, "create_item", item=numbered(MANUAL, 103), after=bad)
    listed = await board.call(AS_OPERATOR, "list_items")
    assert [entry["id"] for entry in listed["items"]] == [first]


async def test_done_and_answer_are_refused_on_a_waiting_item(board: Board) -> None:
    first = await board.create(MANUAL)
    command = await board.create(numbered(MANUAL, 102), after=[first])
    question = await board.create(numbered(DECISION, 103), after=[first])
    with pytest.raises(ToolError, match=f"item {command} waits on {first} and cannot be closed"):
        await board.call(AS_OPERATOR, "done", item_id=command)
    with pytest.raises(ToolError, match=f"item {question} waits on {first} and cannot be closed"):
        await board.call(AS_OPERATOR, "answer", item_id=question, option=1)
    for item_id in (command, question):
        assert (await board.call(AS_OPERATOR, "get_item", item_id=item_id))["open"] is True

    await board.call(AS_OPERATOR, "done", item_id=first)
    await board.call(AS_OPERATOR, "done", item_id=command)
    await board.call(AS_OPERATOR, "answer", item_id=question, option=1)


async def test_reject_names_what_it_closed_down_the_chain(
    board: Board, caplog: pytest.LogCaptureFixture
) -> None:
    first = await board.create(MANUAL)
    second = await board.create(numbered(DECISION, 102), after=[first])
    third = (
        await board.call(AS_NIGHTLY, "create_keyed_item", key=KEY, item=KEYED, after=[second])
    )["item"]["id"]
    bystander = await board.create(numbered(MANUAL, 104))

    caplog.set_level(logging.INFO, logger="factory_board.server")
    rejected = await board.call(AS_OPERATOR, "reject", item_id=first, reason="the press is sold")
    assert rejected["status"] == "rejected; 2 items that waited on it are closed too"
    assert rejected["also_closed"] == [
        {"id": second, "confirmation": f"decision for issue {REPO}#102: choose one of 2 options"},
        {"id": third, "confirmation": f"manual for finding {KEY}: free text: open the vent"},
    ]
    lines = [record.getMessage() for record in caplog.records]
    assert lines == [
        f"[BOARD] item={first} kind=manual verb=reject actor={OPERATOR.identity}",
        f"[BOARD] item={second} kind=decision verb=cascade actor={OPERATOR.identity}",
        f"[BOARD] item={third} kind=manual verb=cascade actor={OPERATOR.identity}",
    ]
    for item_id in (second, third):
        closed = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
        assert (closed["resolution"], closed["result"]) == (
            "superseded",
            {"cause": "after", "item": first, "resolution": "rejected"},
        )
    listed = await board.call(AS_OPERATOR, "list_items")
    assert [entry["id"] for entry in listed["items"]] == [bystander]

    alone = await board.call(AS_OPERATOR, "reject", item_id=bystander, reason="nor this")
    assert (alone["status"], alone["also_closed"]) == ("rejected", [])


async def test_a_reply_that_closes_names_what_it_closed_too(
    board: Board, caplog: pytest.LogCaptureFixture
) -> None:
    first = await board.create(MANUAL)
    second = await board.create(numbered(MANUAL, 102), after=[first])
    held_elsewhere = await board.keyed()
    after_that = await board.create(numbered(MANUAL, 103), after=[held_elsewhere])

    caplog.set_level(logging.INFO, logger="factory_board.server")
    replied = await board.call(AS_OPERATOR, "reply", item_id=first, text="did it another way")
    assert replied["status"] == "replied and closed; 1 item that waited on it is closed too"
    assert [entry["id"] for entry in replied["also_closed"]] == [second]
    closed = await board.call(AS_OPERATOR, "get_item", item_id=second)
    assert closed["result"] == {"cause": "after", "item": first, "resolution": "replied"}
    assert [record.getMessage().split()[3] for record in caplog.records] == [
        "verb=reply",
        "verb=cascade",
    ]

    # A reply that leaves its item open closes nothing after it.
    stays = await board.call(AS_OPERATOR, "reply", item_id=held_elsewhere, text="vent is stuck")
    assert stays["also_closed"] == []
    waiting = await board.call(AS_OPERATOR, "get_item", item_id=after_that)
    assert (waiting["open"], waiting["waiting_on"]) == (True, [held_elsewhere])


async def test_every_create_and_verb_logs_one_line_without_item_text(
    board: Board, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="factory_board.server")
    item_id = await board.create(MANUAL)
    await board.call(AS_SESSION, "create_item", item=MANUAL | {"summary": "Another one."})
    await board.call(AS_OPERATOR, "bump", item_id=item_id)
    await board.call(AS_OPERATOR, "reply", item_id=item_id, text="did it by hand")
    await board.call(AS_OPERATOR, "list_items")
    lines = [record.getMessage() for record in caplog.records]
    assert lines[0] == f"[BOARD] item={item_id} kind=manual verb=create actor={SESSION.identity}"
    assert [line.split()[3] for line in lines] == [
        "verb=create",
        "verb=create",
        "verb=bump",
        "verb=reply",
    ]
    assert not any("widgets" in line or "by hand" in line for line in lines)


async def test_the_metrics_count_items_and_carry_no_text(board: Board, store: Store) -> None:
    first = await board.create(MANUAL)
    await board.create(DECISION)
    await board.call(AS_OPERATOR, "bump", item_id=first)
    deferred = await board.create(MANUAL | {"summary": "Later."})
    await board.call(AS_OPERATOR, "defer", item_id=deferred, until=AHEAD)
    await board.call(AS_OPERATOR, "done", item_id=await board.create(MANUAL | {"summary": "X."}))
    for summary in ("Then this.", "And this."):
        await board.create(MANUAL | {"summary": summary}, after=[first])

    await board.call(AS_SESSION, "create_item", item=numbered(MANUAL, 102), holds=False)

    metrics.show(await store.stats(Caller(Tier.PROCESS, "process:metrics")))

    def sample(name: str, **labels: str) -> float | None:
        return REGISTRY.get_sample_value(name, labels)

    def issue_items(number: int) -> float | None:
        return sample("factory_board_issue_open_items", repo=REPO, issue=str(number))

    def open_items(urgent: str, deferred: str, waiting: str = "false") -> float | None:
        return sample(
            "factory_board_open_items",
            assignee="operator",
            urgent=urgent,
            deferred=deferred,
            waiting=waiting,
        )

    # The decision, and the item on the other issue that doesn't hold it.
    assert open_items("false", "false") == 2
    assert open_items("true", "false") == 1
    assert open_items("false", "true") == 1
    assert open_items("false", "false", "true") == 2
    assert open_items("true", "false", "true") is None
    assert sample("factory_board_closed_items", assignee="operator", resolution="done") == 1
    # The five open ones are on one issue; the item that doesn't hold adds no series.
    assert issue_items(101) == 5
    assert issue_items(102) is None
    oldest = sample(
        "factory_board_oldest_open_item_created_timestamp_seconds",
        assignee="operator",
        urgent="true",
        deferred="false",
        waiting="false",
    )
    assert oldest is not None and abs(oldest - datetime.now(UTC).timestamp()) < 300

    # With nothing open, the operator's plain series reads 0 and the others are gone.
    # From the end of the list: the waiting ones go before the item whose reject would
    # close them.
    listed = await board.call(AS_OPERATOR, "list_items", include_deferred=True)
    for entry in reversed(listed["items"]):
        await board.call(AS_OPERATOR, "reject", item_id=entry["id"], reason="clearing")
    metrics.show(await store.stats(Caller(Tier.PROCESS, "process:metrics")))
    assert open_items("false", "false") == 0
    assert open_items("true", "false") is None
    assert open_items("false", "false", "true") is None
    assert issue_items(101) is None


# ---- the reconciler ----


async def pipeline(board: Board, **arguments: Any) -> list[dict[str, Any]]:
    return (await board.call(AS_RECONCILER, "list_pipeline_items", **arguments))["items"]


async def test_the_two_processes_do_not_share_their_tools(board: Board) -> None:
    assert who(AS_NIGHTLY) == "nightly"
    assert who(AS_RECONCILER) == "reconciler"
    assert who(Identity(RUN, operator_typed=False)) == "session"
    with pytest.raises(ToolError, match="not a tool of this caller's tier"):
        await board.call(AS_NIGHTLY, "write_stage_item", item=ASSESS)
    with pytest.raises(ToolError, match="not a tool of this caller's tier"):
        await board.call(AS_RECONCILER, "create_keyed_item", key=KEY, item=KEYED)


async def test_a_stage_item_is_held_by_nobody_and_open_once(board: Board) -> None:
    created = await board.call(AS_RECONCILER, "write_stage_item", item=REVIEW, urgent=True)
    item = created["item"]
    assert (created["status"], created["confirmation"]) == ("created", f"review for pr {REPO}#202")
    assert (item["assignee"], item["held_by"], item["urgent"]) == ("review", None, True)
    assert item["fields"] == REVIEW
    again = await board.call(AS_RECONCILER, "write_stage_item", item=REVIEW | {"head_sha": SHA_B})
    assert (again["status"], again["item"]["id"]) == ("already open", item["id"])
    # The open item keeps the commit it was written for: a new head is a close and a write.
    assert again["item"]["fields"]["head_sha"] == SHA_A
    # Not on the operator's list, and in the queue for every reader.
    assert (await board.call(AS_OPERATOR, "list_items"))["items"] == []
    for reader in (AS_OPERATOR, AS_SESSION, AS_RECONCILER):
        listed = await board.call(reader, "list_pipeline_items")
        assert [entry["id"] for entry in listed["items"]] == [item["id"]]


@pytest.mark.parametrize(
    "bad",
    [
        {"item": REVIEW, "reasons": ["new"]},
        {"item": REVIEW, "closed_as": "nothing-to-deploy"},
        {"item": ASSESS, "reasons": ["new", "new"]},
        {"item": ASSESS, "reasons": ["because"]},
        {"item": ASSESS, "not_before": "2999-01-01T00:00:00"},
        {"item": ASSESS | {"note": "one\ntwo"}},
        {"item": REVIEW | {"head_sha": "abc"}},
        {"item": {"kind": "retro"}},
        {"item": MANUAL},
        {"item": ASSESS, "claimed_by": RECONCILER.identity},
    ],
)
async def test_a_stage_item_is_checked(board: Board, bad: dict[str, Any]) -> None:
    with pytest.raises(ToolError):
        await board.call(AS_RECONCILER, "write_stage_item", **bad)
    assert await pipeline(board) == []


async def test_a_second_trigger_merges_into_the_open_assess_item(board: Board) -> None:
    later = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    sooner = datetime.now(UTC) + timedelta(minutes=10)
    first = await board.call(
        AS_RECONCILER, "write_stage_item", item=ASSESS, not_before=later, reasons=["new"]
    )
    item_id = first["item"]["id"]
    merged = await board.call(
        AS_RECONCILER,
        "write_stage_item",
        item=ASSESS,
        not_before=sooner.isoformat(),
        reasons=["changed", "new"],
    )
    assert (merged["status"], merged["item"]["id"]) == ("already open", item_id)
    assert merged["item"]["reasons"] == ["new", "changed"]
    assert datetime.fromisoformat(merged["item"]["deferred_until"]) == sooner
    # A later date does not move it back, and no date is the earliest.
    again = await board.call(AS_RECONCILER, "write_stage_item", item=ASSESS, not_before=later)
    assert datetime.fromisoformat(again["item"]["deferred_until"]) == sooner
    now = await board.call(AS_RECONCILER, "write_stage_item", item=ASSESS, reasons=["reopened"])
    assert now["item"]["deferred_until"] is None
    assert now["item"]["reasons"] == ["new", "changed", "reopened"]
    assert [entry["id"] for entry in await pipeline(board)] == [item_id]


async def test_a_trigger_does_nothing_to_an_assess_item_a_run_holds(
    board: Board, conn: Any
) -> None:
    created = await board.call(AS_RECONCILER, "write_stage_item", item=ASSESS, reasons=["new"])
    item_id = created["item"]["id"]
    conn.execute("UPDATE items SET claimed_by = %s WHERE id = %s", (RUN.identity, item_id))
    again = await board.call(AS_RECONCILER, "write_stage_item", item=ASSESS, reasons=["changed"])
    assert (again["item"]["held_by"], again["item"]["reasons"]) == (RUN.identity, ["new"])
    with pytest.raises(ToolError, match=f"held by {RUN.identity}"):
        await board.call(AS_RECONCILER, "close_for_fact", item_id=item_id, cause="issue-closed")


async def test_a_deploy_check_can_be_written_closed(board: Board) -> None:
    created = await board.call(
        AS_RECONCILER, "write_stage_item", item=DEPLOY_CHECK, closed_as="nothing-to-deploy"
    )
    item = created["item"]
    assert (item["open"], item["resolution"]) == (False, "done")
    assert item["result"] == {"outcome": "nothing-to-deploy"}
    assert await pipeline(board) == []
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert [entry["id"] for entry in await pipeline(board, closed_since=since)] == [item["id"]]
    assert await pipeline(board, closed_since=since, repo="example/other") == []


async def test_the_reconciler_holds_a_pr_item_and_closes_it_for_the_fact(board: Board) -> None:
    created = await board.call(AS_RECONCILER, "write_operator_item", item=PR_APPROVE)
    item_id = created["item"]["id"]
    assert created["item"]["held_by"] == RECONCILER.identity
    assert [entry["id"] for entry in (await board.call(AS_OPERATOR, "list_items"))["items"]] == [
        item_id
    ]
    with pytest.raises(ToolError, match=f"held by {RECONCILER.identity}"):
        await board.call(AS_OPERATOR, "done", item_id=item_id)
    closed = await board.call(
        AS_RECONCILER, "close_for_fact", item_id=item_id, cause="approved", superseded=False
    )
    assert closed["status"] == "done"
    item = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
    assert (item["resolution"], item["result"]) == ("done", {"cause": "approved"})


async def test_the_reconciler_s_other_items_are_the_operator_s(board: Board) -> None:
    created = await board.call(AS_RECONCILER, "write_operator_item", item=DECISION)
    item_id = created["item"]["id"]
    assert created["item"]["held_by"] == OPERATOR.identity
    assert created["item"]["operator_typed"] is False
    # It sees what it wrote, and can't close it.
    assert [entry["id"] for entry in await pipeline(board)] == [item_id]
    with pytest.raises(ToolError, match=f"held by {OPERATOR.identity}"):
        await board.call(AS_RECONCILER, "close_for_fact", item_id=item_id, cause="pr-reopened")
    # A session's item is not in the reconciler's list.
    await board.create(numbered(MANUAL, 102))
    assert [entry["id"] for entry in await pipeline(board)] == [item_id]


async def test_a_stage_item_closes_as_superseded_only(board: Board) -> None:
    created = await board.call(AS_RECONCILER, "write_stage_item", item=REVIEW)
    item_id = created["item"]["id"]
    with pytest.raises(ToolError, match="closed as done by its run only"):
        await board.call(
            AS_RECONCILER, "close_for_fact", item_id=item_id, cause="merged", superseded=False
        )
    with pytest.raises(ToolError):
        await board.call(AS_RECONCILER, "close_for_fact", item_id=item_id, cause="not a name")
    await board.call(AS_RECONCILER, "close_for_fact", item_id=item_id, cause="head-moved")
    item = await board.call(AS_RECONCILER, "get_item", item_id=item_id)
    assert (item["resolution"], item["result"]) == ("superseded", {"cause": "head-moved"})
    # The subject is free again.
    again = await board.call(AS_RECONCILER, "write_stage_item", item=REVIEW)
    assert again["status"] == "created"


async def test_the_last_assessment_of_each_issue(board: Board, conn: Any) -> None:
    assert (await board.call(AS_RECONCILER, "list_assessments", repo=REPO))["items"] == []
    ids: list[str] = []
    for input_hash in ("h1", "h2"):
        created = await board.call(AS_RECONCILER, "write_stage_item", item=ASSESS)
        ids.append(created["item"]["id"])
        conn.execute(
            "UPDATE items SET closed_at = now(), resolution = 'done', input_hash = %s, "
            "result = %s WHERE id = %s",
            (input_hash, '{"outcome": "none"}', ids[-1]),
        )
    other = ASSESS | {"issue": {"repo": REPO, "number": 102}}
    await board.call(AS_RECONCILER, "write_stage_item", item=other)
    listed = (await board.call(AS_SESSION, "list_assessments", repo=REPO))["items"]
    assert [(entry["id"], entry["input_hash"]) for entry in listed] == [(ids[1], "h2")]
    assert listed[0]["result"] == {"outcome": "none"}


# ---- the dispatcher ----

WORK: dict[str, Any] = {
    "kind": "work",
    "repo": REPO,
    "number": 203,
    "head_sha": SHA_A,
    "cause": "conflict",
}
REVIEW_LABEL = f"review for pr {REPO}#202"


async def staged(board: Board, item: dict[str, Any], **arguments: Any) -> str:
    """A stage's item, as the reconciler writes it; its ID."""
    created = await board.call(AS_RECONCILER, "write_stage_item", item=item, **arguments)
    assert created["status"] == "created", created
    return created["item"]["id"]


async def claim(board: Board, item_id: str, run_id: str, **arguments: Any) -> dict[str, Any]:
    """Claim as the dispatcher, from the seat the run's ID names unless one is given."""
    arguments = {"seat": seat_of(run_id)} | arguments
    return await board.call(
        AS_DISPATCHER, "claim", item_id=item_id, run_id=run_id, cap_minutes=30, **arguments
    )


async def queue(board: Board) -> dict[str, Any]:
    return await board.call(AS_DISPATCHER, "list_claimable")


def lapse(conn: Any, item_id: str) -> None:
    """Move time: the item's running claim ran out a minute ago."""
    conn.execute(
        "UPDATE items SET claim_expires = now() - interval '1 minute' WHERE id = %s", (item_id,)
    )
    conn.execute(
        "UPDATE claims SET expires_at = now() - interval '1 minute' "
        "WHERE item_id = %s AND ended_at IS NULL",
        (item_id,),
    )


async def run_out(board: Board, conn: Any, item_id: str, run_id: str) -> str | None:
    """One claim that runs out and is counted by the next read of the queue; the decision
    item, if one was written."""
    await claim(board, item_id, run_id)
    lapse(conn, item_id)
    expired = (await queue(board))["expired"]
    assert [(entry["item"], entry["run"]) for entry in expired] == [(item_id, run_id)]
    return expired[0]["decision"]


async def test_the_three_processes_do_not_share_their_tools(board: Board) -> None:
    assert who(AS_DISPATCHER) == "dispatcher"
    item_id = await staged(board, REVIEW)
    for identity in (AS_RECONCILER, AS_NIGHTLY, AS_SESSION, AS_OPERATOR):
        with pytest.raises(ToolError, match="not a tool of this caller's tier"):
            await board.call(
                identity, "claim", item_id=item_id, run_id="r1", cap_minutes=30, seat="review/1"
            )
    with pytest.raises(ToolError, match="not a tool of this caller's tier"):
        await board.call(AS_DISPATCHER, "close_for_fact", item_id=item_id, cause="merged")
    # It reads what the reconciler reads.
    assert [entry["id"] for entry in await pipeline(board)] == [item_id]
    listed = await board.call(AS_DISPATCHER, "list_pipeline_items")
    assert [entry["id"] for entry in listed["items"]] == [item_id]
    assert (await board.call(AS_DISPATCHER, "list_assessments", repo=REPO))["items"] == []


async def test_the_queue_lists_what_ran_out_the_items_and_the_last_claims(
    board: Board, conn: Any
) -> None:
    assert await queue(board) == {
        "expired": [],
        "items": [],
        "last_claims": [],
        "running": [],
        "paused": None,
    }
    review = await staged(board, REVIEW)
    assess = await staged(board, ASSESS)
    check = await staged(board, DEPLOY_CHECK, urgent=True)
    await staged(board, {"kind": "merge", "repo": REPO, "number": 204, "head_sha": SHA_A})
    await board.create(MANUAL)

    listed = await queue(board)
    assert (listed["expired"], listed["last_claims"]) == ([], [])
    assert [(entry["id"], entry["rank"]) for entry in listed["items"]] == [
        (check, 1),
        (review, 4),
        (assess, 6),
    ]
    # An item as every list shows it, with its place in the order.
    shown = await board.call(AS_DISPATCHER, "get_item", item_id=review)
    assert listed["items"][1] == {k: v for k, v in shown.items() if k != "replies"} | {"rank": 4}

    await claim(board, review, "20260101-review-01")
    await claim(board, assess, "20260101-assess-01", input_hash="h1")
    listed = await queue(board)
    assert [entry["id"] for entry in listed["items"]] == [check]
    last = listed["last_claims"]
    assert [(c["run"], c["item"], c["stage"], c["urgent"], c["ending"]) for c in last] == [
        ("20260101-assess-01", assess, "assess", False, None),
        ("20260101-review-01", review, "review", False, None),
    ]
    assert set(last[0]) == {
        "run",
        "item",
        "stage",
        "seat",
        "urgent",
        "claimed_at",
        "expires",
        "ending",
    }
    assert [(c["run"], c["seat"]) for c in listed["running"]] == [
        ("20260101-review-01", "review/1"),
        ("20260101-assess-01", "assess/1"),
    ]

    # The claims that ran out are counted before the queue is read.
    lapse(conn, review)
    listed = await queue(board)
    assert listed["expired"] == [{"item": review, "run": "20260101-review-01", "decision": None}]
    assert [(entry["id"], entry.get("expiries")) for entry in listed["items"]] == [
        (check, None),
        (review, 1),
    ]
    assert [(c["run"], c["ending"]) for c in listed["last_claims"]] == [
        ("20260101-assess-01", None),
        ("20260101-review-01", "expired"),
    ]
    assert (await queue(board))["expired"] == []


async def test_a_claim_answers_with_the_run_s_token_once(
    board: Board, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    item_id = await staged(board, REVIEW)
    claimed = await board.call(
        AS_DISPATCHER,
        "claim",
        item_id=item_id,
        run_id="20260101-review-01",
        cap_minutes=45,
        seat="review/1",
    )
    assert (claimed["status"], claimed["confirmation"]) == ("claimed", REVIEW_LABEL)
    token = claimed["token"]
    assert isinstance(token, str) and len(token) >= 43
    assert claimed["item"]["held_by"] == "run:20260101-review-01"
    ends = datetime.fromisoformat(claimed["item"]["claim_expires"])
    assert abs(ends - datetime.now(UTC) - timedelta(minutes=55)) < timedelta(minutes=1)

    hashed = hashlib.sha256(token.encode()).hexdigest()
    for reader in (AS_DISPATCHER, AS_RECONCILER, AS_SESSION, AS_OPERATOR):
        shown = await board.call(reader, "get_item", item_id=item_id)
        assert shown["held_by"] == "run:20260101-review-01"
        assert shown["claim_expires"] == claimed["item"]["claim_expires"]
        assert shown["claims"] == [
            {
                "run": "20260101-review-01",
                "item": item_id,
                "stage": "review",
                "seat": "review/1",
                "urgent": False,
                "claimed_at": shown["claims"][0]["claimed_at"],
                "expires": shown["claim_expires"],
                "ending": None,
            }
        ]
        # The token was the claim's answer and is nowhere else: not on the item ...
        assert token not in json.dumps(shown) and hashed not in json.dumps(shown)
    listed = await queue(board)
    assert token not in json.dumps(listed) and hashed not in json.dumps(listed)
    # ... and not in the log, which has one line for the claim.
    assert token not in caplog.text and hashed not in caplog.text
    lines = [r.getMessage() for r in caplog.records if r.name == "factory_board.server"]
    assert lines == [
        f"[BOARD] item={item_id} kind=review verb=create actor={RECONCILER.identity}",
        f"[BOARD] item={item_id} kind=review verb=claim actor={DISPATCHER.identity}",
    ]

    with pytest.raises(ToolError, match="is not claimable: it is held by run:20260101-review-01"):
        await claim(board, item_id, "20260101-review-02")
    other = await staged(board, REVIEW | {"number": 205})
    with pytest.raises(ToolError, match="has claimed before"):
        await claim(board, other, "20260101-review-01")
    with pytest.raises(ToolError, match="no item abcdefgh to claim"):
        await claim(board, "abcdefgh", "20260101-review-02")
    again = await claim(board, other, "20260101-review-02")
    assert again["token"] != token


@pytest.mark.parametrize(
    "bad",
    [
        {"run_id": "not a run"},
        {"run_id": ""},
        {"run_id": "run:20260101-review-01"},
        {"cap_minutes": 0},
        {"cap_minutes": 1441},
        {"cap_minutes": "long"},
        {"input_hash": "has space"},
        {"input_hash": ""},
        {"token": "mine"},
        {"claimed_by": "run:20260101-review-09"},
        {"seat": "assess"},
        {"seat": "assess/0"},
        {"seat": "review/1"},
        {"seat": "assess/urgent-1"},
        {"seat": None},
    ],
)
async def test_a_claim_is_checked(board: Board, bad: dict[str, Any]) -> None:
    item_id = await staged(board, ASSESS)
    good = {
        "item_id": item_id,
        "run_id": "20260101-assess-01",
        "cap_minutes": 30,
        "input_hash": "h1",
        "seat": "assess/1",
    }
    arguments = good | bad
    with pytest.raises(ToolError):
        await board.call(AS_DISPATCHER, "claim", **arguments)
    assert (await board.call(AS_DISPATCHER, "get_item", item_id=item_id))["held_by"] is None


async def test_a_stale_work_item_is_closed_and_nothing_is_claimed(
    board: Board, conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    assess = await staged(board, ASSESS)
    with pytest.raises(ToolError, match="claimed with the issue's hash"):
        await claim(board, assess, "20260101-assess-01")
    stamped = await claim(board, assess, "20260101-assess-01", input_hash="h2")
    assert (stamped["status"], stamped["item"]["input_hash"]) == ("claimed", "h2")

    work = await staged(board, WORK)
    conn.execute("UPDATE items SET input_hash = 'h1' WHERE id = %s", (work,))
    caplog.set_level(logging.INFO, logger="factory_board.server")
    stale = await claim(board, work, "20260101-work-01", input_hash="h2")
    assert (stale["status"], stale["confirmation"]) == ("stale", f"work for pr {REPO}#203")
    assert "token" not in stale
    item = stale["item"]
    assert (item["open"], item["resolution"], item["held_by"]) == (False, "superseded", None)
    assert item["result"] == {"cause": "stale", "input_hash": "h2"}
    lines = [r.getMessage() for r in caplog.records if r.name == "factory_board.server"]
    assert lines == [f"[BOARD] item={work} kind=work verb=stale actor={DISPATCHER.identity}"]
    shown = await board.call(AS_DISPATCHER, "get_item", item_id=work)
    assert "claims" not in shown and "claim_expires" not in shown
    listed = await queue(board)
    assert [c["run"] for c in listed["last_claims"]] == ["20260101-assess-01"]
    assert listed["items"] == []

    # Under the hash it was assessed with, it is claimed.
    fresh = await staged(board, WORK)
    conn.execute("UPDATE items SET input_hash = 'h1' WHERE id = %s", (fresh,))
    claimed = await claim(board, fresh, "20260101-work-02", input_hash="h1")
    assert (claimed["status"], claimed["item"]["input_hash"]) == ("claimed", "h1")
    assert "token" in claimed


async def test_a_release_takes_one_of_five_reasons(
    board: Board, caplog: pytest.LogCaptureFixture
) -> None:
    item_id = await staged(board, REVIEW)
    reasons = ["start-failed", "infra-window", "usage-limit", "stopped", "not-live"]
    caplog.set_level(logging.INFO, logger="factory_board.server")
    for count, reason in enumerate(reasons, start=1):
        await claim(board, item_id, f"20260101-review-0{count}")
        released = await board.call(AS_DISPATCHER, "release", item_id=item_id, reason=reason)
        assert (released["status"], released["confirmation"]) == ("released", REVIEW_LABEL)
        item = released["item"]
        assert (item["held_by"], item["releases"], item["open"]) == (None, count, True)
        assert "claim_expires" not in item and "expiries" not in item
        # Back in the queue at once.
        assert [entry["id"] for entry in (await queue(board))["items"]] == [item_id]
    assert [record.getMessage().split()[3] for record in caplog.records] == [
        "verb=claim",
        "verb=release",
    ] * 5

    await claim(board, item_id, "20260101-review-06")
    for bad in ("bored", "Stopped", "", "expired"):
        with pytest.raises(ToolError):
            await board.call(AS_DISPATCHER, "release", item_id=item_id, reason=bad)
    shown = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
    assert shown["held_by"] == "run:20260101-review-06"
    assert [(c["ending"], c.get("reason")) for c in shown["claims"]] == [
        *[("released", reason) for reason in reasons],
        (None, None),
    ]
    assert all("ended_at" in c for c in shown["claims"][:5])
    assert "ended_at" not in shown["claims"][5]


async def test_a_release_of_what_no_run_holds_is_refused(board: Board, conn: Any) -> None:
    item_id = await staged(board, REVIEW)
    with pytest.raises(ToolError, match="held by nobody, not by a run"):
        await board.call(AS_DISPATCHER, "release", item_id=item_id, reason="stopped")
    with pytest.raises(ToolError, match="no item abcdefgh"):
        await board.call(AS_DISPATCHER, "release", item_id="abcdefgh", reason="stopped")
    # A claim that ran out is not given back: it expires.
    await claim(board, item_id, "20260101-review-01")
    lapse(conn, item_id)
    with pytest.raises(ToolError, match="has run out; it can only expire"):
        await board.call(AS_DISPATCHER, "release", item_id=item_id, reason="stopped")
    assert (await queue(board))["expired"][0]["item"] == item_id


async def test_a_deploy_check_is_released_until_a_time(board: Board) -> None:
    check = await staged(board, DEPLOY_CHECK)
    review = await staged(board, REVIEW)
    await claim(board, check, "20260101-deploy-check-01")
    await claim(board, review, "20260101-review-01")
    soon = datetime.now(UTC) + timedelta(hours=2)

    async def release(item_id: str, not_before: str) -> dict[str, Any]:
        return await board.call(
            AS_DISPATCHER, "release", item_id=item_id, reason="not-live", not_before=not_before
        )

    with pytest.raises(ToolError, match="not_before needs a UTC offset"):
        await release(check, soon.replace(tzinfo=None).isoformat())
    with pytest.raises(ToolError, match="only a deploy-check"):
        await release(review, soon.isoformat())
    for bad in (timedelta(hours=-1), timedelta(hours=25)):
        with pytest.raises(ToolError, match="at most a day ahead"):
            await release(check, (datetime.now(UTC) + bad).isoformat())
    for item_id in (check, review):
        assert (await board.call(AS_DISPATCHER, "get_item", item_id=item_id))["held_by"]

    released = await release(check, soon.isoformat())
    item = released["item"]
    assert (item["held_by"], item["releases"]) == (None, 1)
    assert datetime.fromisoformat(item["deferred_until"]) == soon
    assert (await queue(board))["items"] == []
    shown = await board.call(AS_RECONCILER, "get_item", item_id=check)
    assert [(c["ending"], c["reason"]) for c in shown["claims"]] == [("released", "not-live")]


async def twice_run_out(board: Board, conn: Any) -> tuple[str, str]:
    """A review item whose claim ran out twice in a row, and the decision written for it."""
    item_id = await staged(board, REVIEW)
    assert await run_out(board, conn, item_id, "20260101-review-01") is None
    decision = await run_out(board, conn, item_id, "20260101-review-02")
    assert decision is not None
    return item_id, decision


async def test_two_expiries_in_a_row_ask_the_operator(
    board: Board, conn: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="factory_board.server")
    item_id, decision = await twice_run_out(board, conn)
    lines = [record.getMessage() for record in caplog.records if "verb=expire" in record.message]
    actor = f"actor={DISPATCHER.identity}"
    assert lines == [
        f"[BOARD] item={item_id} verb=expire run=20260101-review-01 decision=- {actor}",
        f"[BOARD] item={item_id} verb=expire run=20260101-review-02 decision={decision} {actor}",
    ]

    listed = (await board.call(AS_OPERATOR, "list_items"))["items"]
    assert [entry["id"] for entry in listed] == [decision]
    asked = listed[0]
    assert (asked["kind"], asked["subject"], asked["urgent"]) == (
        "decision",
        f"pr {REPO}#202",
        False,
    )
    assert (asked["held_by"], asked["created_by"]) == (OPERATOR.identity, DISPATCHER.identity)
    assert asked["operator_typed"] is False
    assert len(asked["fields"]["options"]) == 2
    for named in ("review", item_id, "20260101-review-01", "20260101-review-02"):
        assert named in asked["summary"]

    # The item waits on the decision: it is in no queue, and no run is started on it.
    waiting = await board.call(AS_DISPATCHER, "get_item", item_id=item_id)
    assert (waiting["open"], waiting["held_by"], waiting["expiries"]) == (True, None, 2)
    assert waiting["waiting_on"] == [decision]
    assert [(c["run"], c["ending"], c["decision"]) for c in waiting["claims"]] == [
        ("20260101-review-01", "expired", decision),
        ("20260101-review-02", "expired", decision),
    ]
    assert await queue(board) == {
        "expired": [],
        "items": [],
        "last_claims": (await queue(board))["last_claims"],
        "running": [],
        "paused": None,
    }
    with pytest.raises(ToolError, match="is not claimable"):
        await claim(board, item_id, "20260101-review-03")


@pytest.mark.parametrize(
    ("given", "answer"),
    [
        ({"option": 2}, "Stop: close the item as superseded"),
        ({"text": "sell the press"}, "sell the press"),
    ],
    ids=["stop", "own-text"],
)
async def test_any_answer_but_try_again_closes_the_item_with_the_decision(
    board: Board, conn: Any, caplog: pytest.LogCaptureFixture, given: dict[str, Any], answer: str
) -> None:
    item_id, decision = await twice_run_out(board, conn)
    caplog.set_level(logging.INFO, logger="factory_board.server")
    answered = await board.call(AS_OPERATOR, "answer", item_id=decision, **given)
    assert answered == {
        "status": "answered",
        "confirmation": f"decision for pr {REPO}#202: choose one of 2 options: {answer}",
        "also_closed": [{"id": item_id, "confirmation": REVIEW_LABEL}],
    }
    assert [record.getMessage() for record in caplog.records] == [
        f"[BOARD] item={decision} kind=decision verb=answer actor={OPERATOR.identity}",
        f"[BOARD] item={item_id} kind=review verb=cascade actor={OPERATOR.identity}",
    ]
    closed = await board.call(AS_OPERATOR, "get_item", item_id=item_id)
    assert (closed["open"], closed["resolution"]) == (False, "superseded")
    assert closed["result"] == {"cause": "after", "item": decision, "resolution": "answered"}
    assert (await queue(board))["items"] == []
    # The subject is free: the reconciler writes the next review item.
    assert await staged(board, REVIEW) != item_id


async def test_try_again_puts_the_item_back_in_the_queue(board: Board, conn: Any) -> None:
    item_id, decision = await twice_run_out(board, conn)
    answered = await board.call(AS_OPERATOR, "answer", item_id=decision, option=1)
    assert answered["status"] == "answered"
    assert answered["confirmation"].endswith(
        ": Try again: the item goes back in the queue, for two more runs"
    )
    assert answered["also_closed"] == []
    listed = await queue(board)
    assert [(entry["id"], entry["rank"], entry["expiries"]) for entry in listed["items"]] == [
        (item_id, 4, 2)
    ]
    assert "waiting_on" not in listed["items"][0]
    # Two more runs, and the operator is asked again.
    assert await run_out(board, conn, item_id, "20260101-review-03") is None
    second = await run_out(board, conn, item_id, "20260101-review-04")
    assert second not in (None, decision)
    assert [e["id"] for e in (await board.call(AS_OPERATOR, "list_items"))["items"]] == [second]


async def test_a_reject_of_the_decision_closes_the_item_too(board: Board, conn: Any) -> None:
    item_id, decision = await twice_run_out(board, conn)
    rejected = await board.call(AS_OPERATOR, "reject", item_id=decision, reason="not now")
    assert rejected["also_closed"] == [{"id": item_id, "confirmation": REVIEW_LABEL}]


async def test_an_answer_to_any_other_decision_closes_nothing_else(board: Board) -> None:
    first = await board.create(DECISION)
    await board.create(numbered(MANUAL, 102), after=[first])
    answered = await board.call(AS_OPERATOR, "answer", item_id=first, option=2)
    assert answered["also_closed"] == []
