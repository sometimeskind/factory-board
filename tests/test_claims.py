"""The claims through the store: the dispatcher's methods, and what each caller is told.

The rules themselves are tested with plain SQL in test_claims_sql.py. Every repo, number,
run and hash here is invented.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from helpers import (
    DISPATCHER,
    NIGHTLY,
    OPERATOR,
    RECONCILER,
    RUN,
    SESSION,
    issue,
    manual,
    pr,
    stage,
)

from factory_board.store import (
    Caller,
    Claim,
    Claimable,
    Expired,
    InvalidItem,
    Item,
    ItemClosed,
    ItemNotFound,
    NotClaimable,
    NotPermitted,
    Resolution,
    RunToken,
    Store,
    Tier,
    token_sha256,
)

OTHER_RUN = Caller(Tier.AGENT, "run:20260101-work-02")
RUN_ID = RUN.identity.partition(":")[2]


async def review(store: Store, number: int = 202) -> Item:
    """An open `review` item on a made-up PR, held by nobody."""
    new = replace(stage("review"), subject=pr(number))
    return (await store.create_item(RECONCILER, new)).item


def lapse(conn: psycopg.Connection, item_id: str) -> None:
    """Move time: the item's running claim ran out a minute ago."""
    conn.execute(
        "UPDATE items SET claim_expires = now() - interval '1 minute' WHERE id = %s", (item_id,)
    )
    conn.execute(
        "UPDATE claims SET expires_at = now() - interval '1 minute' "
        "WHERE item_id = %s AND ended_at IS NULL",
        (item_id,),
    )


async def run_out(store: Store, conn: psycopg.Connection, item_id: str, run: str) -> Expired:
    """One claim that runs out and is counted."""
    await store.claim_item(DISPATCHER, item_id, run, 30)
    lapse(conn, item_id)
    expired = await store.expire_claims(DISPATCHER, OPERATOR.identity)
    assert [(entry.item_id, entry.run) for entry in expired] == [(item_id, run)]
    return expired[0]


async def test_the_queue_is_ranked_then_oldest(store: Store) -> None:
    work = (await store.create_item(RECONCILER, stage("work", 101))).item
    retro = (await store.create_item(RECONCILER, replace(stage("retro", 101), urgent=True))).item
    assess = (await store.create_item(RECONCILER, stage("assess", 101))).item
    later = (await store.create_item(RECONCILER, stage("assess", 102))).item
    reviewed = await review(store)
    # In no queue: the operator's, the merge script's, a deferred one and one a session holds.
    await store.create_item(SESSION, manual(103))
    await store.create_item(RECONCILER, replace(stage("merge"), subject=pr(202)))
    ahead = datetime.now(UTC) + timedelta(days=1)
    await store.create_item(RECONCILER, replace(stage("work", 104), not_before=ahead))
    await store.create_item(SESSION, stage("work", 105, held_by=SESSION.identity))

    expected = [
        Claimable(retro, 1),
        Claimable(reviewed, 4),
        Claimable(assess, 6),
        Claimable(later, 6),
        Claimable(work, 7),
    ]
    for caller in (DISPATCHER, OPERATOR, SESSION, RECONCILER):
        assert await store.list_claimable(caller) == expected

    await store.claim_item(DISPATCHER, reviewed.id, "20260101-review-01", 30)
    assert await store.list_claimable(DISPATCHER) == [expected[0], *expected[2:]]


async def test_a_claim_returns_the_run_s_token_once(store: Store) -> None:
    item = await review(store)
    claimed = await store.claim_item(DISPATCHER, item.id, "20260101-review-01", 45)
    assert claimed.stale is False
    assert claimed.token is not None and len(claimed.token) >= 43
    held = claimed.item
    assert held.claimed_by == "run:20260101-review-01"
    assert held.claim_expires is not None
    assert abs(held.claim_expires - datetime.now(UTC) - timedelta(minutes=55)) < timedelta(
        minutes=1
    )
    assert held == replace(item, claimed_by=held.claimed_by, claim_expires=held.claim_expires)
    assert await store.get_item(OPERATOR, item.id) == held

    assert await store.run_for_token(claimed.token) == RunToken(
        "20260101-review-01", item.id, "review"
    )
    for other in ("", "not-the-token", claimed.token + "x", token_sha256(claimed.token)):
        assert await store.run_for_token(other) is None

    # Another claim mints another token.
    second = await review(store, 203)
    again = await store.claim_item(DISPATCHER, second.id, "20260101-review-02", 45)
    assert again.token is not None and again.token != claimed.token
    assert await store.run_for_token(again.token) == RunToken(
        "20260101-review-02", second.id, "review"
    )


async def test_a_claim_keeps_the_hash_of_the_token_and_not_the_token(
    store: Store, conn: psycopg.Connection
) -> None:
    item = await review(store)
    claimed = await store.claim_item(DISPATCHER, item.id, "20260101-review-01", 45)
    assert claimed.token is not None
    rows = conn.execute("SELECT token_sha256 FROM claims").fetchall()
    assert rows == [(token_sha256(claimed.token),)]
    everything = conn.execute("SELECT row_to_json(c)::text FROM claims AS c").fetchall()
    stored = everything + conn.execute("SELECT row_to_json(i)::text FROM items AS i").fetchall()
    assert not any(claimed.token in text for (text,) in stored)


@pytest.mark.parametrize(
    "caller", [RECONCILER, NIGHTLY, SESSION, RUN, OPERATOR], ids=lambda c: c.identity
)
async def test_only_the_dispatcher_claims_and_counts(store: Store, caller: Caller) -> None:
    item = await review(store)
    with pytest.raises(NotPermitted):
        await store.claim_item(caller, item.id, "20260101-review-01", 30)
    with pytest.raises(NotPermitted):
        await store.expire_claims(caller, OPERATOR.identity)
    assert await store.get_item(OPERATOR, item.id) == item
    assert await store.list_claims(OPERATOR, item.id) == []


async def test_what_a_claim_is_refused_for(store: Store) -> None:
    item = await review(store)
    await store.claim_item(DISPATCHER, item.id, "20260101-review-01", 30)
    with pytest.raises(NotClaimable, match="held by run:20260101-review-01"):
        await store.claim_item(DISPATCHER, item.id, "20260101-review-02", 30)
    with pytest.raises(NotClaimable, match="no item zzzzzzzz"):
        await store.claim_item(DISPATCHER, "zzzzzzzz", "20260101-review-02", 30)
    operators = (await store.create_item(SESSION, manual(101))).item
    with pytest.raises(NotClaimable, match="not claimable"):
        await store.claim_item(DISPATCHER, operators.id, "20260101-review-02", 30)

    other = await review(store, 203)
    with pytest.raises(InvalidItem, match="has claimed before"):
        await store.claim_item(DISPATCHER, other.id, "20260101-review-01", 30)
    for run, cap in (("not a run", 30), ("", 30), ("20260101-review-02", 0)):
        with pytest.raises(InvalidItem):
            await store.claim_item(DISPATCHER, other.id, run, cap)
    with pytest.raises(InvalidItem, match="only an assess item"):
        await store.claim_item(DISPATCHER, other.id, "20260101-review-02", 30, "h1")
    assert await store.get_item(OPERATOR, other.id) == other

    await store.close_item(RECONCILER, other.id, Resolution.SUPERSEDED, {"cause": "merged"})
    with pytest.raises(ItemClosed):
        await store.claim_item(DISPATCHER, other.id, "20260101-review-02", 30)


async def test_an_assess_item_is_stamped_and_a_stale_work_item_is_closed(store: Store) -> None:
    assess = (await store.create_item(RECONCILER, stage("assess", 101))).item
    with pytest.raises(InvalidItem, match="claimed with the issue's hash"):
        await store.claim_item(DISPATCHER, assess.id, "20260101-assess-01", 30)
    claimed = await store.claim_item(DISPATCHER, assess.id, "20260101-assess-01", 30, "h2")
    assert (claimed.stale, claimed.item.input_hash) == (False, "h2")

    fresh = replace(stage("work", 102), input_hash="h1")
    same = (await store.create_item(RECONCILER, fresh)).item
    kept = await store.claim_item(DISPATCHER, same.id, "20260101-work-01", 30, "h1")
    assert (kept.stale, kept.item.claimed_by) == (False, "run:20260101-work-01")
    assert kept.token is not None

    stale = (await store.create_item(RECONCILER, replace(fresh, subject=issue(103)))).item
    closed = await store.claim_item(DISPATCHER, stale.id, "20260101-work-02", 30, "h2")
    assert closed.stale is True and closed.token is None
    assert (closed.item.is_open, closed.item.resolution) == (False, Resolution.SUPERSEDED)
    assert closed.item.result == {"cause": "stale", "input_hash": "h2"}
    assert (closed.item.claimed_by, closed.item.input_hash) == (None, "h1")
    assert await store.list_claims(DISPATCHER, stale.id) == []
    # The subject is free for the next work item.
    again = await store.create_item(RECONCILER, replace(fresh, subject=issue(103)))
    assert again.existing is False


async def test_a_release_gives_the_item_back(store: Store) -> None:
    item = await review(store)
    claimed = await store.claim_item(DISPATCHER, item.id, "20260101-review-01", 30)
    assert claimed.token is not None
    released = await store.release_claim(DISPATCHER, item.id, "usage-limit")
    assert released == replace(item, releases=1)
    assert await store.run_for_token(claimed.token) is None
    assert [entry.item.id for entry in await store.list_claimable(DISPATCHER)] == [item.id]
    claims = await store.list_claims(OPERATOR, item.id)
    assert [(c.run, c.ending, c.reason) for c in claims] == [
        ("20260101-review-01", "released", "usage-limit")
    ]
    assert claims[0].ended_at is not None

    with pytest.raises(NotClaimable, match="held by nobody, not by a run"):
        await store.release_claim(DISPATCHER, item.id, "stopped")
    with pytest.raises(ItemNotFound):
        await store.release_claim(DISPATCHER, "zzzzzzzz", "stopped")
    await store.claim_item(DISPATCHER, item.id, "20260101-review-02", 30)
    with pytest.raises(InvalidItem, match="short lower-case name"):
        await store.release_claim(DISPATCHER, item.id, "Not A Name")
    with pytest.raises(InvalidItem, match="only a deploy-check"):
        await store.release_claim(
            DISPATCHER, item.id, "not-live", datetime.now(UTC) + timedelta(hours=1)
        )
    assert (await store.get_item(OPERATOR, item.id)).claimed_by == "run:20260101-review-02"


async def test_a_run_releases_its_own_claim_and_no_other(store: Store) -> None:
    item = await review(store)
    await store.claim_item(DISPATCHER, item.id, RUN_ID, 30)
    for caller in (OTHER_RUN, SESSION, NIGHTLY, RECONCILER, OPERATOR):
        with pytest.raises(NotPermitted):
            await store.release_claim(caller, item.id, "stopped")
    released = await store.release_claim(RUN, item.id, "stopped")
    assert (released.claimed_by, released.releases) == (None, 1)


async def test_a_deploy_check_is_released_until_a_later_time(store: Store) -> None:
    new = replace(stage("deploy-check"), subject=pr(202))
    item = (await store.create_item(RECONCILER, new)).item
    await store.claim_item(DISPATCHER, item.id, "20260101-deploy-check-01", 30)
    for bad in (timedelta(minutes=-1), timedelta(hours=25)):
        with pytest.raises(InvalidItem, match="at most a day ahead"):
            await store.release_claim(DISPATCHER, item.id, "not-live", datetime.now(UTC) + bad)
    when = datetime.now(UTC) + timedelta(hours=2)
    released = await store.release_claim(DISPATCHER, item.id, "not-live", when)
    assert (released.not_before, released.releases, released.claimed_by) == (when, 1, None)
    assert await store.list_claimable(DISPATCHER) == []


async def test_a_claim_that_ran_out_expires_and_is_not_released(
    store: Store, conn: psycopg.Connection
) -> None:
    item = await review(store)
    running = await review(store, 203)
    claimed = await store.claim_item(DISPATCHER, item.id, RUN_ID, 30)
    await store.claim_item(DISPATCHER, running.id, "20260101-review-02", 30)
    assert claimed.token is not None
    assert await store.expire_claims(DISPATCHER, OPERATOR.identity) == []

    lapse(conn, item.id)
    # Dead before it is counted: the token, the release, and the run's own close.
    assert await store.run_for_token(claimed.token) is None
    for caller in (DISPATCHER, RUN):
        with pytest.raises(NotClaimable, match="can only expire"):
            await store.release_claim(caller, item.id, "stopped")
    with pytest.raises(NotPermitted):
        await store.close_item(RUN, item.id, Resolution.DONE, {"verdict": "approve"})

    expired = await store.expire_claims(DISPATCHER, OPERATOR.identity)
    assert expired == [Expired(item.id, RUN_ID, None)]
    assert await store.get_item(OPERATOR, item.id) == replace(item, expiries=1)
    assert [entry.item.id for entry in await store.list_claimable(DISPATCHER)] == [item.id]
    assert await store.expire_claims(DISPATCHER, OPERATOR.identity) == []
    with pytest.raises(InvalidItem, match="held by a person"):
        await store.expire_claims(DISPATCHER, "process:dispatcher")


async def test_a_run_closes_its_item_within_its_claim(store: Store) -> None:
    item = await review(store)
    claimed = await store.claim_item(DISPATCHER, item.id, RUN_ID, 30)
    assert claimed.token is not None
    with pytest.raises(NotPermitted):
        await store.close_item(OTHER_RUN, item.id, Resolution.DONE)
    closed = await store.close_item(RUN, item.id, Resolution.DONE, {"verdict": "approve"})
    assert (closed.resolution, closed.claimed_by) == (Resolution.DONE, RUN.identity)
    assert await store.run_for_token(claimed.token) is None
    claims = await store.list_claims(OPERATOR, item.id)
    assert [(c.run, c.ending, c.ended_at) for c in claims] == [(RUN_ID, "closed", closed.closed_at)]


async def test_two_expiries_in_a_row_write_a_decision_the_item_waits_on(
    store: Store, conn: psycopg.Connection
) -> None:
    item = (await store.create_item(RECONCILER, replace(stage("work", 101), urgent=True))).item
    assert (await run_out(store, conn, item.id, "20260101-work-01")).decision_id is None
    second = await run_out(store, conn, item.id, "20260101-work-02")
    assert second.decision_id is not None
    assert re.fullmatch(r"[a-hjkmnp-z2-9]{8}", second.decision_id)

    decision = await store.get_item(OPERATOR, second.decision_id)
    assert (decision.assignee, decision.kind, decision.subject) == (
        "operator",
        "decision",
        issue(101),
    )
    assert (decision.claimed_by, decision.created_by) == (OPERATOR.identity, DISPATCHER.identity)
    assert (decision.urgent, decision.operator_typed, decision.key) == (True, False, None)
    assert decision.fields["ticket"] == {"repo": "example/repo", "number": 101}
    assert [entry.id for entry in await store.list_open(OPERATOR, assignee="operator")] == [
        decision.id
    ]

    waiting = await store.get_item(OPERATOR, item.id)
    assert (waiting.expiries, waiting.claimed_by) == (2, None)
    assert waiting.after == waiting.waiting_on == (decision.id,)
    assert await store.list_claimable(DISPATCHER) == []
    with pytest.raises(NotClaimable):
        await store.claim_item(DISPATCHER, item.id, "20260101-work-03", 30)

    claims = await store.list_claims(SESSION, item.id)
    assert [(c.run, c.item_id, c.stage, c.urgent, c.ending, c.decision_id) for c in claims] == [
        ("20260101-work-01", item.id, "work", True, "expired", decision.id),
        ("20260101-work-02", item.id, "work", True, "expired", decision.id),
    ]
    assert all(c.ended_at is not None and c.reason is None for c in claims)


async def test_the_last_claims_are_the_newest_first(store: Store, conn: psycopg.Connection) -> None:
    assert await store.last_claims(DISPATCHER) == []
    assess = (await store.create_item(RECONCILER, stage("assess", 101))).item
    work = (await store.create_item(RECONCILER, replace(stage("work", 102), urgent=True))).item
    reviewed = await review(store)
    await store.claim_item(DISPATCHER, assess.id, "20260101-assess-01", 30, "h1")
    await store.release_claim(DISPATCHER, assess.id, "infra-window")
    await store.claim_item(DISPATCHER, work.id, RUN_ID, 30)
    await store.close_item(RUN, work.id, Resolution.DONE)
    await run_out(store, conn, reviewed.id, "20260101-review-01")
    await store.claim_item(DISPATCHER, assess.id, "20260101-assess-02", 30, "h1")

    def told(claims: list[Claim]) -> list[tuple[str, str, bool, str | None, str | None]]:
        return [(c.run, c.stage, c.urgent, c.ending, c.reason) for c in claims]

    everything = [
        ("20260101-assess-02", "assess", False, None, None),
        ("20260101-review-01", "review", False, "expired", None),
        (RUN_ID, "work", True, "closed", None),
        ("20260101-assess-01", "assess", False, "released", "infra-window"),
    ]
    assert told(await store.last_claims(DISPATCHER)) == everything[:2]
    assert told(await store.last_claims(DISPATCHER, limit=10)) == everything
    for caller in (OPERATOR, SESSION, RECONCILER):
        assert told(await store.last_claims(caller, limit=1)) == everything[:1]
    # One item's claims, the oldest first.
    assert told(await store.list_claims(DISPATCHER, assess.id)) == [everything[3], everything[0]]
    assert await store.list_claims(DISPATCHER, "zzzzzzzz") == []


async def decided(store: Store, conn: psycopg.Connection) -> tuple[Item, str]:
    """A `review` item that ran out twice, and the decision item written for it."""
    item = await review(store)
    await run_out(store, conn, item.id, "20260101-review-01")
    decision = (await run_out(store, conn, item.id, "20260101-review-02")).decision_id
    assert decision is not None
    return item, decision


async def test_try_again_lets_the_item_go_and_closes_nothing(
    store: Store, conn: psycopg.Connection
) -> None:
    item, decision = await decided(store, conn)
    await store.close_item(
        OPERATOR, decision, Resolution.ANSWERED, {"option": 1, "answer": "Try again"}
    )
    assert await store.list_cascaded(OPERATOR, decision) == []
    again = await store.get_item(OPERATOR, item.id)
    assert (again.is_open, again.after, again.waiting_on) == (True, (decision,), ())
    assert [entry.item.id for entry in await store.list_claimable(DISPATCHER)] == [item.id]
    # Two more runs, and then the second decision.
    assert (await run_out(store, conn, item.id, "20260101-review-03")).decision_id is None
    second = (await run_out(store, conn, item.id, "20260101-review-04")).decision_id
    assert second is not None and second != decision


@pytest.mark.parametrize(
    "result", [{"option": 2, "answer": "Stop"}, {"answer": "sell the press"}], ids=["stop", "text"]
)
async def test_any_other_answer_closes_the_item_and_the_cascade_names_it(
    store: Store, conn: psycopg.Connection, result: dict[str, Any]
) -> None:
    item, decision = await decided(store, conn)
    await store.close_item(OPERATOR, decision, Resolution.ANSWERED, result)
    closed = await store.get_item(OPERATOR, item.id)
    assert (closed.resolution, closed.result) == (
        Resolution.SUPERSEDED,
        {"cause": "after", "item": decision, "resolution": "answered"},
    )
    assert await store.list_cascaded(OPERATOR, decision) == [closed]
    assert await store.list_claimable(DISPATCHER) == []


async def test_a_reject_of_the_decision_closes_the_item_too(
    store: Store, conn: psycopg.Connection
) -> None:
    item, decision = await decided(store, conn)
    await store.close_item(OPERATOR, decision, Resolution.REJECTED, {"reason": "not now"})
    closed = await store.get_item(OPERATOR, item.id)
    assert closed.result == {"cause": "after", "item": decision, "resolution": "rejected"}
    assert await store.list_cascaded(OPERATOR, decision) == [closed]
