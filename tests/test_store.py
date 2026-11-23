"""The store, as each tier, against a real Postgres through board_app."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import chain, repeat

import psycopg
import pytest
from helpers import (
    NIGHTLY,
    OPERATOR,
    OTHER_SESSION,
    RECONCILER,
    RUN,
    SESSION,
    SHA_A,
    SHA_B,
    issue,
    keyed,
    manual,
    pr,
    pr_approve,
    stage,
)
from psycopg import errors

from factory_board.store import (
    ID_ALPHABET,
    Caller,
    InvalidItem,
    ItemClosed,
    ItemNotFound,
    NewItem,
    NotClaimHolder,
    NotPermitted,
    Pool,
    Resolution,
    Store,
    StoreError,
    Subject,
    Tier,
    make_pool,
    new_item_id,
)

EVERY_CALLER = [OPERATOR, SESSION, RUN, RECONCILER]


async def count(pool: Pool) -> int:
    """Rows in items, counted as the operator on the pool's one connection."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SET LOCAL ROLE board_operator")
        row = await (await conn.execute("SELECT count(*) FROM items")).fetchone()
    assert row is not None
    return row[0]


# Callers and IDs


def test_an_identity_must_fit_its_tier() -> None:
    for tier, identity in [
        (Tier.OPERATOR, "session:0a1b2c3d"),
        (Tier.AGENT, "human:operator"),
        (Tier.AGENT, "process:reconciler"),
        (Tier.PROCESS, "run:20260101-work-01"),
        (Tier.PROCESS, "reconciler"),
    ]:
        with pytest.raises(ValueError, match="identity starts with"):
            Caller(tier, identity)


def test_a_new_id_is_eight_characters_without_look_alikes() -> None:
    ids = {new_item_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(re.fullmatch(r"[a-hjkmnp-z2-9]{8}", item_id) for item_id in ids)
    assert not set("01ilo") & set(ID_ALPHABET)
    assert len(set(ID_ALPHABET)) == len(ID_ALPHABET) == 31


# Create, get and list


async def test_create_then_get(store: Store) -> None:
    new = NewItem(
        assignee="operator",
        kind="manual",
        subject=issue(101),
        fields={"command": "make widgets"},
        machine="workstation",
        claimed_by=OPERATOR.identity,
        urgent=True,
        created_run="0a1b2c3d",
        operator_typed=True,
    )
    created = await store.create_item(SESSION, new)
    assert created.existing is False
    item = created.item
    assert re.fullmatch(r"[a-hjkmnp-z2-9]{8}", item.id)
    assert item.subject == Subject("issue", "101", "example/repo")
    assert (item.assignee, item.kind, item.machine) == ("operator", "manual", "workstation")
    assert item.fields == {"command": "make widgets"}
    assert item.claimed_by == "human:operator"
    assert item.created_by == "session:0a1b2c3d"
    assert (item.created_run, item.operator_typed, item.urgent) == ("0a1b2c3d", True, True)
    assert item.is_open and item.resolution is None and item.result is None
    assert (item.expiries, item.releases, item.reasons, item.input_hash) == (0, 0, (), None)
    assert item.claim_expires is None and item.not_before is None
    assert abs(datetime.now(UTC) - item.created_at) < timedelta(minutes=1)
    for caller in EVERY_CALLER:
        assert await store.get_item(caller, item.id) == item


async def test_get_of_a_missing_item(store: Store) -> None:
    with pytest.raises(ItemNotFound):
        await store.get_item(OPERATOR, "zzzzzzzz")


@pytest.mark.parametrize(
    "new",
    [
        NewItem(assignee="somebody", kind="manual", subject=issue(101)),
        NewItem(assignee="operator", kind="wish", subject=issue(101)),
        NewItem(assignee="operator", kind="manual", subject=issue(101), claimed_by="operator"),
        NewItem(assignee="operator", kind="manual", subject=issue(101), machine="toaster"),
        NewItem(assignee="operator", kind="manual"),
    ],
)
async def test_create_of_a_bad_item_is_refused(store: Store, pool: Pool, new: NewItem) -> None:
    with pytest.raises(InvalidItem, match="refused by items_"):
        await store.create_item(SESSION, new)
    assert await count(pool) == 0


async def test_a_bad_id_is_refused(pool: Pool) -> None:
    with pytest.raises(InvalidItem, match="items_id_format"):
        await Store(pool, new_id=lambda: "NOT-AN-ID").create_item(SESSION, manual())


async def test_an_id_collision_is_retried(pool: Pool) -> None:
    ids = chain(["aaaaaaaa", "aaaaaaaa", "aaaaaaaa", "bbbbbbbb"], repeat("cccccccc"))
    store = Store(pool, new_id=lambda: next(ids))
    first = await store.create_item(OPERATOR, manual(101))
    second = await store.create_item(OPERATOR, manual(102))
    assert (first.item.id, first.existing) == ("aaaaaaaa", False)
    assert (second.item.id, second.existing) == ("bbbbbbbb", False)
    assert second.item.subject == issue(102)
    assert await store.get_item(OPERATOR, "aaaaaaaa") == first.item


async def test_an_id_that_never_frees_gives_up(pool: Pool) -> None:
    store = Store(pool, new_id=lambda: "aaaaaaaa")
    await store.create_item(OPERATOR, manual(101))
    with pytest.raises(StoreError, match="no free item ID"):
        await store.create_item(OPERATOR, manual(102))
    assert await count(pool) == 1


async def test_list_open(store: Store) -> None:
    later = datetime.now(UTC) + timedelta(days=3)
    earlier = datetime.now(UTC) - timedelta(days=3)
    first = (await store.create_item(SESSION, manual(101))).item
    urgent = (await store.create_item(SESSION, replace(manual(102), urgent=True))).item
    deferred = (await store.create_item(SESSION, replace(manual(103), not_before=later))).item
    due = (await store.create_item(SESSION, replace(manual(104), not_before=earlier))).item
    closed = (await store.create_item(SESSION, manual(105))).item
    work = (await store.create_item(SESSION, stage("work", 101))).item
    await store.close_item(OPERATOR, closed.id, Resolution.DONE)

    # Urgent first, then the oldest; nothing deferred, nothing closed.
    assert await store.list_open(OPERATOR) == [urgent, first, due, work]
    assert await store.list_open(RECONCILER, assignee="operator") == [urgent, first, due]
    assert await store.list_open(SESSION, assignee="work") == [work]
    assert await store.list_open(SESSION, assignee="review") == []
    assert await store.list_open(OPERATOR, assignee="operator", include_deferred=True) == [
        urgent,
        first,
        deferred,
        due,
    ]
    everything = await store.list_open(OPERATOR, include_deferred=True)
    assert closed.id not in [item.id for item in everything]
    assert len(everything) == 5


# The uniqueness rules: a create is safe to run twice


async def test_the_operator_gets_several_items_on_one_issue(store: Store, pool: Pool) -> None:
    first = await store.create_item(SESSION, manual(101))
    second = await store.create_item(SESSION, manual(101))
    third = await store.create_item(
        OTHER_SESSION,
        NewItem(assignee="operator", kind="decision", subject=issue(101), fields={"options": []}),
    )
    assert [c.existing for c in (first, second, third)] == [False, False, False]
    assert len({first.item.id, second.item.id, third.item.id}) == 3
    assert await count(pool) == 3


async def test_a_second_open_stage_item_returns_the_first(store: Store, pool: Pool) -> None:
    first = await store.create_item(RECONCILER, stage("assess", 101))
    again = await store.create_item(
        RUN, replace(stage("assess", 101), urgent=True, reasons=("edited",), input_hash="h2")
    )
    assert again.existing is True
    # The item that was there, untouched by the second create.
    assert again.item == first.item
    assert again.item.created_by == "process:reconciler"
    assert await count(pool) == 1
    # Another stage, another issue and another repo are not in its way.
    for other in (
        stage("work", 101),
        stage("assess", 102),
        replace(stage("assess", 101), subject=issue(101, "example/other")),
    ):
        assert (await store.create_item(RECONCILER, other)).existing is False
    assert await count(pool) == 4


async def test_a_stage_item_can_follow_a_closed_one(store: Store) -> None:
    first = await store.create_item(RECONCILER, stage("assess", 101, held_by=RUN.identity))
    await store.close_item(RUN, first.item.id, Resolution.DONE, {"outcome": "work"})
    second = await store.create_item(RECONCILER, stage("assess", 101))
    assert second.existing is False
    assert second.item.id != first.item.id


async def test_a_second_open_pr_item_returns_the_first(store: Store, pool: Pool) -> None:
    first = await store.create_item(SESSION, pr_approve(202))
    again = await store.create_item(OTHER_SESSION, pr_approve(202))
    assert (again.existing, again.item) == (True, first.item)
    # One open item of either kind per PR.
    as_merge = await store.create_item(RECONCILER, pr_approve(202, kind="pr-merge"))
    assert (as_merge.existing, as_merge.item) == (True, first.item)
    assert await count(pool) == 1
    assert (await store.create_item(SESSION, pr_approve(203))).existing is False
    other_repo = replace(pr_approve(202), subject=pr(202, "example/other"))
    assert (await store.create_item(SESSION, other_repo)).existing is False


@pytest.mark.parametrize("caller", [SESSION, RECONCILER, OPERATOR], ids=lambda c: c.identity)
async def test_a_second_pr_item_with_a_new_head_moves_the_sha_only(
    store: Store, pool: Pool, caller: Caller
) -> None:
    """A second push: the open item the operator holds gets the new head, and nothing else."""
    first = (await store.create_item(SESSION, pr_approve(202, SHA_A))).item
    newer = replace(
        pr_approve(202, SHA_B),
        fields={"head_sha": SHA_B, "why": "a different reason"},
        urgent=True,
        machine="workstation",
    )
    again = await store.create_item(caller, newer)
    assert again.existing is True
    assert again.item == replace(first, fields={**first.fields, "head_sha": SHA_B})
    assert await store.get_item(OPERATOR, first.id) == again.item
    assert await count(pool) == 1


async def test_a_pr_item_can_follow_a_closed_one(store: Store) -> None:
    first = await store.create_item(SESSION, pr_approve(202, SHA_A))
    await store.close_item(OPERATOR, first.item.id, Resolution.SUPERSEDED)
    second = await store.create_item(SESSION, pr_approve(202, SHA_B))
    assert second.existing is False
    assert second.item.fields["head_sha"] == SHA_B
    assert (await store.get_item(SESSION, first.item.id)).fields["head_sha"] == SHA_A


async def test_a_second_open_item_per_key_returns_the_first(store: Store, pool: Pool) -> None:
    first = await store.create_item(NIGHTLY, keyed("finding:widget-press-hot"))
    again = await store.create_item(
        NIGHTLY, replace(keyed("finding:widget-press-hot"), fields={"options": ["other"]})
    )
    assert (again.existing, again.item) == (True, first.item)
    assert first.item.subject is None
    assert (await store.create_item(NIGHTLY, keyed("finding:widget-press-cold"))).existing is False
    assert await count(pool) == 2

    await store.close_item(OPERATOR, first.item.id, Resolution.ANSWERED, {"answer": "cool it"})
    after = await store.create_item(NIGHTLY, keyed("finding:widget-press-hot"))
    assert after.existing is False
    assert after.item.id != first.item.id
    assert await count(pool) == 3


async def test_two_creates_at_once_make_one_item(migrated: None, app_url: str) -> None:
    """Two connections insert the same stage item together: one wins, the other gets it back."""
    async with make_pool(app_url, min_size=4, max_size=4) as several:
        store = Store(several)
        created = await asyncio.gather(
            *(store.create_item(RECONCILER, stage("assess", 101)) for _ in range(8))
        )
        assert sorted(c.existing for c in created) == [False] + [True] * 7
        assert len({c.item.id for c in created}) == 1
        assert await count(several) == 1


# Close: an item is closed by its claim holder


@pytest.mark.parametrize(
    ("closer", "holder", "allowed"),
    [
        (OPERATOR, "human:operator", True),
        (OPERATOR, "process:reconciler", True),
        (OPERATOR, "session:0a1b2c3d", True),
        (OPERATOR, None, True),
        (SESSION, "human:operator", False),
        (SESSION, "process:reconciler", False),
        (SESSION, "session:0a1b2c3d", True),
        (SESSION, "session:9z8y7x6w", True),
        (RUN, "human:operator", False),
        (RUN, "run:20260101-work-01", True),
        (RECONCILER, "process:reconciler", True),
        (RECONCILER, "process:nightly", False),
        (RECONCILER, "human:operator", False),
        (RECONCILER, "session:0a1b2c3d", False),
        (NIGHTLY, "process:nightly", True),
        (NIGHTLY, "process:reconciler", False),
    ],
    ids=lambda v: v.identity if isinstance(v, Caller) else str(v),
)
async def test_who_may_close_whose_item(
    store: Store, closer: Caller, holder: str | None, allowed: bool
) -> None:
    item = (await store.create_item(SESSION, manual(101, held_by=holder))).item
    if not allowed:
        with pytest.raises(NotClaimHolder, match=f"item {item.id} is held by {holder}"):
            await store.close_item(closer, item.id, Resolution.DONE, {"note": "widgets made"})
        assert await store.get_item(OPERATOR, item.id) == item
        return
    closed = await store.close_item(closer, item.id, Resolution.DONE, {"note": "widgets made"})
    assert closed.closed_at is not None and not closed.is_open
    assert closed.resolution is Resolution.DONE
    assert closed.result == {"note": "widgets made"}
    assert closed == replace(
        item, closed_at=closed.closed_at, resolution=Resolution.DONE, result=closed.result
    )
    assert await store.get_item(SESSION, item.id) == closed


@pytest.mark.parametrize("resolution", list(Resolution))
async def test_close_with_each_resolution(store: Store, resolution: Resolution) -> None:
    item = (await store.create_item(SESSION, manual())).item
    closed = await store.close_item(OPERATOR, item.id, resolution)
    assert (closed.resolution, closed.result) == (resolution, None)


def test_there_are_five_resolutions() -> None:
    assert sorted(Resolution) == ["answered", "done", "rejected", "replied", "superseded"]


async def test_close_of_a_missing_item(store: Store) -> None:
    with pytest.raises(ItemNotFound):
        await store.close_item(OPERATOR, "zzzzzzzz", Resolution.DONE)


@pytest.mark.parametrize("caller", [OPERATOR, SESSION], ids=lambda c: c.tier.name)
async def test_a_closed_item_stays_as_it_is(store: Store, caller: Caller) -> None:
    """There is no undo: every write on a closed item is refused, for every tier."""
    item = (await store.create_item(SESSION, pr_approve(202))).item
    closed = await store.close_item(OPERATOR, item.id, Resolution.DONE, {"note": "approved"})
    with pytest.raises(ItemClosed):
        await store.close_item(caller, item.id, Resolution.REJECTED, {"note": "no"})
    with pytest.raises(ItemClosed):
        await store.set_urgent(caller, item.id, True)
    with pytest.raises(ItemClosed):
        await store.set_not_before(caller, item.id, datetime.now(UTC))
    with pytest.raises(ItemClosed):
        await store.set_head_sha(caller, item.id, SHA_B)
    assert await store.get_item(OPERATOR, item.id) == closed


async def test_the_trigger_s_refusal_is_a_closed_item(store: Store, pool: Pool) -> None:
    """A write that gets past the store's own `closed_at IS NULL` still ends as ItemClosed."""
    item = (await store.create_item(SESSION, manual(101, held_by=None))).item
    await store.close_item(SESSION, item.id, Resolution.DONE)
    with pytest.raises(ItemClosed, match="is closed and cannot be changed"):
        async with store._as(SESSION) as conn:  # pyright: ignore[reportPrivateUsage]
            await conn.execute("UPDATE items SET urgent = true WHERE id = %s", (item.id,))


# The other writes follow the same policy


async def test_set_not_before_and_urgent(store: Store) -> None:
    item = (await store.create_item(SESSION, manual())).item
    when = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    deferred = await store.set_not_before(OPERATOR, item.id, when)
    assert deferred == replace(item, not_before=when)
    assert await store.list_open(OPERATOR) == []
    bumped = await store.set_urgent(OPERATOR, item.id, True)
    assert bumped == replace(deferred, urgent=True)
    back = await store.set_not_before(OPERATOR, item.id, None)
    assert back == replace(item, urgent=True)
    assert await store.list_open(OPERATOR) == [back]


async def test_an_agent_cannot_defer_or_bump_the_operator_s_item(store: Store) -> None:
    item = (await store.create_item(SESSION, manual())).item
    with pytest.raises(NotClaimHolder):
        await store.set_urgent(SESSION, item.id, True)
    with pytest.raises(NotClaimHolder):
        await store.set_not_before(RECONCILER, item.id, datetime.now(UTC))
    with pytest.raises(ItemNotFound):
        await store.set_urgent(OPERATOR, "zzzzzzzz", True)
    assert await store.get_item(OPERATOR, item.id) == item


@pytest.mark.parametrize("caller", [SESSION, RUN, RECONCILER, OPERATOR], ids=lambda c: c.identity)
async def test_set_head_sha_changes_only_the_sha(store: Store, caller: Caller) -> None:
    item = (await store.create_item(SESSION, pr_approve(202, SHA_A))).item
    assert item.claimed_by == "human:operator"
    moved = await store.set_head_sha(caller, item.id, SHA_B)
    assert moved == replace(item, fields={**item.fields, "head_sha": SHA_B})
    assert await store.get_item(OPERATOR, item.id) == moved


@pytest.mark.parametrize("caller", [SESSION, RUN, RECONCILER], ids=lambda c: c.identity)
async def test_moving_the_head_sha_is_not_a_way_to_close(store: Store, caller: Caller) -> None:
    item = (await store.create_item(SESSION, pr_approve(202, SHA_A))).item
    moved = await store.set_head_sha(caller, item.id, SHA_B)
    with pytest.raises(NotClaimHolder):
        await store.close_item(caller, item.id, Resolution.DONE)
    with pytest.raises(NotClaimHolder):
        await store.set_urgent(caller, item.id, True)
    assert await store.get_item(OPERATOR, item.id) == moved
    assert moved.is_open


@pytest.mark.parametrize("caller", [SESSION, RECONCILER, OPERATOR], ids=lambda c: c.identity)
async def test_set_head_sha_is_for_open_pr_items(store: Store, caller: Caller) -> None:
    other = (await store.create_item(SESSION, manual(101, held_by=None))).item
    with pytest.raises(InvalidItem, match="not a pr-approve or pr-merge item"):
        await store.set_head_sha(caller, other.id, SHA_B)
    with pytest.raises(ItemNotFound):
        await store.set_head_sha(caller, "zzzzzzzz", SHA_B)
    item = (await store.create_item(SESSION, pr_approve(202, SHA_A))).item
    for bad in ("", "abc", "A" * 40, "a" * 41):
        with pytest.raises(InvalidItem, match="40 lower-case hex"):
            await store.set_head_sha(caller, item.id, bad)
    assert await store.get_item(OPERATOR, other.id) == other
    assert await store.get_item(OPERATOR, item.id) == item


# Replies


async def test_replies(store: Store) -> None:
    item = (await store.create_item(SESSION, stage("work", 101, held_by=RUN.identity))).item
    first = await store.add_reply(OPERATOR, item.id, "use the spare press")
    second = await store.add_reply(OPERATOR, item.id, "and oil it first")
    assert (first.item_id, first.author, first.body) == (
        item.id,
        "human:operator",
        "use the spare press",
    )
    assert first.id != second.id
    for caller in EVERY_CALLER:
        assert await store.list_replies(caller, item.id) == [first, second]
    assert await store.list_replies(OPERATOR, "zzzzzzzz") == []
    # A reply is not a change to the item.
    assert await store.get_item(OPERATOR, item.id) == item


@pytest.mark.parametrize("caller", [SESSION, RUN, RECONCILER], ids=lambda c: c.identity)
async def test_only_the_operator_replies(store: Store, caller: Caller) -> None:
    item = (await store.create_item(SESSION, manual(101, held_by=caller.identity))).item
    with pytest.raises(NotPermitted):
        await store.add_reply(caller, item.id, "done already")
    assert await store.list_replies(OPERATOR, item.id) == []


async def test_a_reply_needs_an_item_and_a_text(store: Store) -> None:
    with pytest.raises(ItemNotFound):
        await store.add_reply(OPERATOR, "zzzzzzzz", "use the spare press")
    item = (await store.create_item(SESSION, manual())).item
    with pytest.raises(InvalidItem, match="replies_body_not_empty"):
        await store.add_reply(OPERATOR, item.id, "")


# The pooled connection: neither the role nor the actor outlives its transaction


async def test_the_store_connects_as_the_app_login_not_the_owner(
    store: Store, pool: Pool, conn: psycopg.Connection
) -> None:
    await store.create_item(SESSION, manual())
    async with pool.connection() as pooled:
        row = await (await pooled.execute("SELECT session_user, current_user")).fetchone()
    assert row == ("board_app", "board_app")
    owner = conn.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'items'")
    assert owner.fetchone() == ("board_owner",)


async def test_the_role_and_the_actor_do_not_leak_to_the_next_transaction(
    store: Store, pool: Pool
) -> None:
    assert pool.max_size == 1  # every call below is on the same connection
    item = (await store.create_item(OPERATOR, manual())).item
    async with pool.connection() as pooled:
        row = await (
            await pooled.execute(
                "SELECT pg_backend_pid(), current_user, current_setting('board.actor', true)"
            )
        ).fetchone()
        assert row is not None
        backend, role, actor = row
        assert role == "board_app"
        assert not actor
        with pytest.raises(errors.InsufficientPrivilege):
            await pooled.execute("SELECT count(*) FROM items")
        await pooled.rollback()

    # The operator's role is gone: on the same connection an agent is refused again.
    with pytest.raises(NotClaimHolder):
        await store.close_item(SESSION, item.id, Resolution.DONE)
    # And the operator's again right after an agent's call is the operator's.
    await store.set_urgent(OPERATOR, item.id, True)

    # The actor is gone too: one process's identity does not carry over to the next.
    mine = (
        await store.create_item(RECONCILER, stage("merge", 101, held_by="process:reconciler"))
    ).item
    with pytest.raises(NotClaimHolder):
        await store.close_item(NIGHTLY, mine.id, Resolution.DONE)
    await store.close_item(RECONCILER, mine.id, Resolution.DONE)

    async with pool.connection() as pooled:
        row = await (
            await pooled.execute(
                "SELECT pg_backend_pid(), current_user, current_setting('board.actor', true)"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == backend
    assert (row[1], row[2] or "") == ("board_app", "")


async def test_a_failed_call_leaves_no_role_behind(store: Store, pool: Pool) -> None:
    with pytest.raises(InvalidItem):
        await store.create_item(OPERATOR, NewItem(assignee="somebody", kind="manual"))
    async with pool.connection() as pooled:
        row = await (await pooled.execute("SELECT current_user")).fetchone()
        assert row == ("board_app",)
