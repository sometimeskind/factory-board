"""The tool surface over a real store: who may call what, and what each verb does.

Every repo, number, name and command here is invented.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from helpers import NIGHTLY, OPERATOR, REPO, SESSION, SHA_A, SHA_B
from prometheus_client import REGISTRY

from factory_board import metrics
from factory_board.auth import Identity
from factory_board.server import build_server
from factory_board.store import Caller, Store, Tier

AS_OPERATOR = Identity(OPERATOR, operator_typed=True)
AS_SESSION = Identity(SESSION, operator_typed=True)
AS_NIGHTLY = Identity(NIGHTLY, operator_typed=False)

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

TOOLS = {
    Tier.OPERATOR: {"list_items", "get_item", "done", "answer", "reply", "defer", "reject", "bump"},
    Tier.AGENT: {"create_item", "list_items", "get_item", "update_pr_head"},
    Tier.PROCESS: {"create_keyed_item", "list_own_items", "close_own_item"},
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


@pytest.mark.parametrize("identity", [AS_OPERATOR, AS_SESSION, AS_NIGHTLY])
async def test_a_tool_outside_the_tier_s_row_is_refused(
    board: Board, store: Store, identity: Identity
) -> None:
    for tool in sorted(set(CALLS) - TOOLS[identity.caller.tier]):
        with pytest.raises(ToolError, match="not a tool of this caller's tier"):
            await board.call(identity, tool, **CALLS[tool])
    assert await store.list_open(OPERATOR, include_deferred=True) == []


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
    assert "Nothing is woken" in replied["note"]
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

    metrics.show(await store.stats(Caller(Tier.PROCESS, "process:metrics")))

    def sample(name: str, **labels: str) -> float | None:
        return REGISTRY.get_sample_value(name, labels)

    def open_items(urgent: str, deferred: str, waiting: str = "false") -> float | None:
        return sample(
            "factory_board_open_items",
            assignee="operator",
            urgent=urgent,
            deferred=deferred,
            waiting=waiting,
        )

    assert open_items("false", "false") == 1
    assert open_items("true", "false") == 1
    assert open_items("false", "true") == 1
    assert open_items("false", "false", "true") == 2
    assert open_items("true", "false", "true") is None
    assert sample("factory_board_closed_items", assignee="operator", resolution="done") == 1
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
