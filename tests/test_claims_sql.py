"""The claims, with plain SQL past the store: what Postgres itself lets each caller do.

Every repo, number, run and hash here is invented. Time is moved by the owner, who sets a
claim's end in the past; nothing here sleeps until one.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

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
    free_seat,
    insert_row,
    insert_stage_row,
    row_of,
    seat_of,
)
from psycopg import errors
from psycopg.types.json import Jsonb

from factory_board.kinds import Decision, Ticket, parse_operator_item
from factory_board.store import Caller, Tier

OTHER_RUN = Caller(Tier.AGENT, "run:20260101-work-02")
# The run ID of helpers.RUN, so that RUN holds what is claimed for it.
RUN_ID = RUN.identity.partition(":")[2]
EVERY_TIER = [OPERATOR, SESSION, DISPATCHER]
NOT_THE_DISPATCHER = [NIGHTLY, RECONCILER, SESSION, RUN, OPERATOR]

CLAIM = "SELECT claim_item(%s, %s, %s, %s, %s, %s, %s)"
RELEASE = "SELECT release_claim(%s, %s, %s, %s)"
EXPIRE = "SELECT * FROM expire_claims(%s)"
TOKEN = "SELECT * FROM run_of_token(%s)"
CLOSE = "UPDATE items SET closed_at = now(), resolution = 'done' WHERE id = %s"
CLOSE_AS = "UPDATE items SET closed_at = now(), resolution = %s, result = %s WHERE id = %s"
QUEUE = (
    "SELECT c.id, c.rank FROM claimable AS c JOIN items AS i ON i.id = c.id "
    "ORDER BY c.rank, i.created_at, i.id"
)
ITEM = "aaaaaaaa"
OTHER = "bbbbbbbb"


@pytest.fixture
def owner(migrated: None, owner_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(owner_url, autocommit=True) as connection:
        yield connection


@pytest.fixture
def item(owner: psycopg.Connection) -> str:
    """An open `review` item that nobody holds."""
    insert_stage_row(owner, ITEM, assignee="review", subject_type="pr", subject_ref="202")
    return ITEM


def sha(token: str) -> str:
    """The hash of a made-up token. A test's token is its run's ID."""
    return hashlib.sha256(token.encode()).hexdigest()


def claim(
    app_url: str,
    item_id: str,
    run: str,
    cap: int = 30,
    current_hash: str | None = None,
    seat: str | None = None,
    seat_type: str = "normal",
) -> str:
    """Claim as the dispatcher, from a free seat unless one is given; the function's answer."""
    with as_tier(app_url, DISPATCHER) as conn:
        row = conn.execute(
            CLAIM, (item_id, run, cap, sha(run), current_hash, seat or free_seat(conn), seat_type)
        ).fetchone()
    assert row is not None
    return row[0]


def release(
    app_url: str,
    item_id: str,
    reason: str = "stopped",
    wake: Any = None,
    by: Caller = DISPATCHER,
    holder: str | None = None,
) -> bool:
    if holder is None and reason == "result-refused":
        holder = OPERATOR.identity
    with as_tier(app_url, by) as conn:
        row = conn.execute(RELEASE, (item_id, reason, wake, holder)).fetchone()
    assert row is not None
    return row[0]


def expire(app_url: str, holder: str = OPERATOR.identity) -> list[tuple[Any, ...]]:
    with as_tier(app_url, DISPATCHER) as conn:
        return conn.execute(EXPIRE, (holder,)).fetchall()


def lapse(owner: psycopg.Connection, item_id: str) -> None:
    """Move time: the item's running claim ran out a minute ago."""
    owner.execute(
        "UPDATE items SET claim_expires = now() - interval '1 minute' WHERE id = %s", (item_id,)
    )
    owner.execute(
        "UPDATE claims SET expires_at = now() - interval '1 minute' "
        "WHERE item_id = %s AND ended_at IS NULL",
        (item_id,),
    )


def run_out(owner: psycopg.Connection, app_url: str, item_id: str, run: str) -> str | None:
    """One claim that runs out and is counted; the decision item, if one was written."""
    assert claim(app_url, item_id, run) == "claimed"
    lapse(owner, item_id)
    rows = expire(app_url)
    assert [(row[0], row[1]) for row in rows] == [(item_id, run)]
    return rows[0][2]


def claims_of(owner: psycopg.Connection, item_id: str) -> list[tuple[Any, ...]]:
    """(run, ending, reason, decision_id) of each of the item's claims, oldest first."""
    return owner.execute(
        "SELECT run, ending, reason, decision_id FROM claims WHERE item_id = %s "
        "ORDER BY claimed_at, run",
        (item_id,),
    ).fetchall()


def state(owner: psycopg.Connection, item_id: str) -> tuple[Any, ...]:
    """(claimed_by, claim_expires, expiries, releases, open) of an item."""
    row = owner.execute(
        "SELECT claimed_by, claim_expires, expiries, releases, closed_at IS NULL "
        "FROM items WHERE id = %s",
        (item_id,),
    ).fetchone()
    assert row is not None
    return row


def queue(app_url: str, caller: Caller = DISPATCHER) -> list[str]:
    with as_tier(app_url, caller) as conn:
        return [row[0] for row in conn.execute(QUEUE).fetchall()]


def at(owner: psycopg.Connection, offset: str) -> datetime:
    """The database's clock, moved by an interval such as `1 hour` or `-1 minute`."""
    row = owner.execute("SELECT now() + %s::interval", (offset,)).fetchone()
    assert row is not None
    return row[0]


def token_owner(app_url: str, token: str) -> tuple[Any, ...] | None:
    """What board_app is told about a token, as itself."""
    with psycopg.connect(app_url, autocommit=True) as conn:
        return conn.execute(TOKEN, (sha(token),)).fetchone()


# Who may call what


@pytest.mark.parametrize("caller", NOT_THE_DISPATCHER, ids=lambda c: c.identity)
def test_only_the_dispatcher_claims_and_counts_expiries(
    app_url: str, owner: psycopg.Connection, item: str, caller: Caller
) -> None:
    before = row_of(owner, item)
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        conn.execute(CLAIM, (item, "20260101-review-01", 30, sha("t"), None, "lisbon", "normal"))
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        conn.execute(EXPIRE, (OPERATOR.identity,))
    assert row_of(owner, item) == before
    assert owner.execute("SELECT count(*) FROM claims").fetchone() == (0,)


@pytest.mark.parametrize("actor", [None, "", "dispatcher", "process:dispatcher2"])
def test_a_process_whose_actor_is_not_the_dispatcher_claims_nothing(
    app_url: str, item: str, actor: str | None
) -> None:
    for statement, arguments in (
        (CLAIM, (item, "20260101-review-01", 30, sha("t"), None, "lisbon", "normal")),
        (EXPIRE, (OPERATOR.identity,)),
        (RELEASE, (item, "stopped", None, None)),
    ):
        with (
            pytest.raises(errors.InsufficientPrivilege),
            psycopg.connect(app_url) as conn,
            conn.transaction(),
        ):
            conn.execute("SET LOCAL ROLE board_process")
            if actor is not None:
                conn.execute("SELECT set_config('board.actor', %s, true)", (actor,))
            conn.execute(statement, arguments)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("caller", [DISPATCHER, RUN], ids=lambda c: c.identity)
def test_the_dispatcher_and_the_holding_run_release_a_claim(
    app_url: str, owner: psycopg.Connection, item: str, caller: Caller
) -> None:
    claim(app_url, item, RUN_ID)
    assert release(app_url, item, by=caller) is True
    assert state(owner, item) == (None, None, 0, 1, True)


@pytest.mark.parametrize(
    "caller", [OTHER_RUN, SESSION, NIGHTLY, OPERATOR], ids=lambda c: c.identity
)
def test_nobody_else_releases_a_run_s_claim(
    app_url: str, owner: psycopg.Connection, item: str, caller: Caller
) -> None:
    claim(app_url, item, RUN_ID)
    before = row_of(owner, item)
    with pytest.raises(errors.InsufficientPrivilege):
        release(app_url, item, by=caller)
    assert row_of(owner, item) == before
    assert claims_of(owner, item) == [(RUN_ID, None, None, None)]


def test_the_app_login_asks_whose_token_it_is_and_no_tier_does(app_url: str, item: str) -> None:
    claim(app_url, item, RUN_ID)
    assert token_owner(app_url, RUN_ID) == (RUN_ID, item, "review")
    for caller in (*EVERY_TIER, RUN, RECONCILER):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(TOKEN, (sha(RUN_ID),))
    # And the app login still reads no claim as itself.
    with (
        pytest.raises(errors.InsufficientPrivilege),
        psycopg.connect(app_url, autocommit=True) as conn,
    ):
        conn.execute("SELECT run FROM claims")


@pytest.mark.parametrize("caller", [*EVERY_TIER, RUN], ids=lambda c: c.identity)
def test_every_tier_reads_the_claims_but_not_a_token_s_hash(
    app_url: str, item: str, caller: Caller
) -> None:
    claim(app_url, item, RUN_ID)
    with as_tier(app_url, caller) as conn:
        rows = conn.execute(
            "SELECT run, item_id, claimed_at IS NOT NULL, expires_at > now(), ended_at, ending, "
            "reason, decision_id FROM claims"
        ).fetchall()
    assert rows == [(RUN_ID, item, True, True, None, None, None, None)]
    for statement in (
        "SELECT token_sha256 FROM claims",
        "SELECT * FROM claims",
        "SELECT run FROM claims WHERE token_sha256 = 'x'",
        "SELECT run FROM claims ORDER BY token_sha256",
    ):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(statement)


@pytest.mark.parametrize("caller", [*EVERY_TIER, RUN], ids=lambda c: c.identity)
def test_no_tier_writes_a_claim(
    app_url: str, owner: psycopg.Connection, item: str, caller: Caller
) -> None:
    claim(app_url, item, RUN_ID)
    before = owner.execute("SELECT * FROM claims").fetchall()
    for statement in (
        "INSERT INTO claims (run, item_id, token_sha256, expires_at) "
        "VALUES ('20260101-review-09', 'aaaaaaaa', repeat('c', 64), now())",
        "UPDATE claims SET expires_at = now() + interval '1 day'",
        "UPDATE claims SET ended_at = now(), ending = 'closed'",
        "UPDATE claims SET token_sha256 = repeat('c', 64)",
        "DELETE FROM claims",
        "TRUNCATE claims",
    ):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(statement)
    assert owner.execute("SELECT * FROM claims").fetchall() == before


@pytest.mark.parametrize("caller", [*EVERY_TIER, RUN], ids=lambda c: c.identity)
def test_no_tier_writes_the_claim_columns_or_the_counters(
    app_url: str, item: str, caller: Caller
) -> None:
    """The dispatcher got no new right on a row: the functions are the only way."""
    for assignment in (
        "claimed_by = 'run:20260101-review-09'",
        "claimed_by = NULL",
        "claim_expires = now() + interval '1 day'",
        "expiries = 0",
        "releases = 0",
        "input_hash = 'h9'",
    ):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(f"UPDATE items SET {assignment} WHERE id = %s", (item,))  # pyright: ignore[reportArgumentType]


def test_the_claims_table_is_forced_under_row_security(owner: psycopg.Connection) -> None:
    rows = owner.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner) "
        "FROM pg_class WHERE relname IN ('items', 'replies', 'item_after', 'claims') "
        "ORDER BY relname"
    ).fetchall()
    assert rows == [
        ("claims", True, True, "board_owner"),
        ("item_after", True, True, "board_owner"),
        ("items", True, True, "board_owner"),
        ("replies", True, True, "board_owner"),
    ]
    grants = owner.execute(
        "SELECT grantee, privilege_type, string_agg(column_name, ',' ORDER BY column_name) "
        "FROM information_schema.column_privileges "
        "WHERE table_name = 'claims' AND grantee <> 'board_owner' "
        "GROUP BY grantee, privilege_type ORDER BY grantee"
    ).fetchall()
    readable = "claimed_at,decision_id,ended_at,ending,expires_at,item_id,reason,run,seat,seat_type"
    assert grants == [
        ("board_agent", "SELECT", readable),
        ("board_operator", "SELECT", readable),
        ("board_process", "SELECT", readable),
    ]


def test_the_claim_functions_are_the_owner_s_and_granted_to_these_only(
    owner: psycopg.Connection,
) -> None:
    rows = owner.execute(
        "SELECT proname, prosecdef, proconfig, pg_get_userbyid(proowner), proacl::text "
        "FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = ANY(%s) "
        "ORDER BY proname",
        (
            [
                "board_decide_after_failed_runs",
                "board_new_item_id",
                "claim_item",
                "claims_end_on_close",
                "expire_claims",
                "items_stop_after_expiries",
                "release_claim",
                "run_of_token",
            ],
        ),
    ).fetchall()
    path = ["search_path=pg_catalog, pg_temp"]
    mine = "board_owner=X/board_owner"
    assert rows == [
        ("board_decide_after_failed_runs", False, path, "board_owner", f"{{{mine}}}"),
        ("board_new_item_id", False, path, "board_owner", f"{{{mine}}}"),
        ("claim_item", True, path, "board_owner", f"{{{mine},board_process=X/board_owner}}"),
        ("claims_end_on_close", True, path, "board_owner", f"{{{mine}}}"),
        ("expire_claims", True, path, "board_owner", f"{{{mine},board_process=X/board_owner}}"),
        ("items_stop_after_expiries", True, path, "board_owner", f"{{{mine}}}"),
        (
            "release_claim",
            True,
            path,
            "board_owner",
            f"{{{mine},board_agent=X/board_owner,board_process=X/board_owner}}",
        ),
        ("run_of_token", True, path, "board_owner", f"{{{mine},board_app=X/board_owner}}"),
    ]


def test_a_board_made_id_is_an_item_id(app_url: str, owner: psycopg.Connection) -> None:
    ids = [
        row[0]
        for row in owner.execute(
            "SELECT board_new_item_id() FROM generate_series(1, 2000)"
        ).fetchall()
    ]
    assert all(re.fullmatch(r"[a-hjkmnp-z2-9]{8}", item_id) for item_id in ids)
    assert len(set(ids)) == 2000
    # Every character of the alphabet turns up, in every place.
    for place in range(8):
        assert len({item_id[place] for item_id in ids}) == 31
    for caller in (*EVERY_TIER, RUN):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute("SELECT board_new_item_id()")


# The claim


def test_a_claim_holds_the_item_for_the_run_until_ten_minutes_past_the_cap(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    before = row_of(owner, item)
    assert claim(app_url, item, "20260101-review-01", cap=45) == "claimed"
    row = owner.execute(
        "SELECT i.claimed_by, i.claim_expires = c.expires_at, "
        "c.expires_at - c.claimed_at, now() - c.claimed_at < interval '1 minute', "
        "c.run, c.token_sha256, c.ended_at, c.ending, c.reason, c.decision_id, "
        "i.expiries, i.releases, i.input_hash "
        "FROM items AS i JOIN claims AS c ON c.item_id = i.id WHERE i.id = %s",
        (item,),
    ).fetchone()
    assert row is not None
    run = "20260101-review-01"
    assert row[:4] == (f"run:{run}", True, timedelta(minutes=55), True)
    assert row[4:] == (run, sha(run), None, None, None, None, 0, 0, None)
    # Nothing else on the row moved.
    columns = [d.name for d in owner.execute("SELECT * FROM items LIMIT 0").description or []]
    after = row_of(owner, item)
    changed = {c for c, b, a in zip(columns, before, after, strict=True) if b != a}
    assert changed == {"claimed_by", "claim_expires"}


def test_a_claimed_item_is_not_claimed_again(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, "20260101-review-01")
    with pytest.raises(psycopg.Error) as refused:
        claim(app_url, item, "20260101-review-02")
    assert refused.value.sqlstate == "BRD03"
    assert "is not claimable: it is held by run:20260101-review-01" in str(refused.value)
    assert claims_of(owner, item) == [("20260101-review-01", None, None, None)]


def refused_items(owner: psycopg.Connection) -> dict[str, str]:
    """One open item per reason an item is in no queue; the key is the reason."""
    ahead = "2999-01-01T00:00:00+00:00"
    insert_stage_row(owner, "cccccccc", claimed_by=SESSION.identity, subject_ref="301")
    insert_stage_row(owner, "dddddddd", not_before=ahead, subject_ref="302")
    insert_stage_row(owner, "eeeeeeee", subject_ref="303")
    insert_row(owner, id="ffffffff", subject_ref="304")
    owner.execute("INSERT INTO item_after (item_id, after_id) VALUES ('eeeeeeee', 'ffffffff')")
    insert_stage_row(owner, "gggggggg", assignee="merge", subject_type="pr", subject_ref="305")
    return {
        "held": "cccccccc",
        "deferred": "dddddddd",
        "waiting": "eeeeeeee",
        "the operator's": "ffffffff",
        "merge": "gggggggg",
    }


@pytest.mark.parametrize("reason", ["held", "deferred", "waiting", "the operator's", "merge"])
def test_an_item_that_is_in_no_queue_is_not_claimed(
    app_url: str, owner: psycopg.Connection, reason: str
) -> None:
    item_id = refused_items(owner)[reason]
    before = row_of(owner, item_id)
    with pytest.raises(psycopg.Error) as refused:
        claim(app_url, item_id, "20260101-work-09")
    assert refused.value.sqlstate == "BRD03"
    assert f"item {item_id} is not claimable" in str(refused.value)
    assert row_of(owner, item_id) == before
    assert owner.execute("SELECT count(*) FROM claims").fetchone() == (0,)


def test_a_closed_or_a_missing_item_is_not_claimed(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    owner.execute(CLOSE, (item,))
    with pytest.raises(psycopg.Error) as refused:
        claim(app_url, item, "20260101-review-01")
    assert refused.value.sqlstate == "BRD01"
    assert "is closed and cannot be changed" in str(refused.value)
    with pytest.raises(psycopg.Error) as refused:
        claim(app_url, "zzzzzzzz", "20260101-review-01")
    assert refused.value.sqlstate == "BRD03"
    assert "no item zzzzzzzz to claim" in str(refused.value)
    assert owner.execute("SELECT count(*) FROM claims").fetchone() == (0,)


def test_an_item_whose_date_has_come_or_whose_wait_is_over_is_claimed(
    app_url: str, owner: psycopg.Connection
) -> None:
    ids = refused_items(owner)
    owner.execute("UPDATE items SET not_before = now() WHERE id = %s", (ids["deferred"],))
    owner.execute(CLOSE, (ids["the operator's"],))
    assert claim(app_url, ids["deferred"], "20260101-work-01") == "claimed"
    assert claim(app_url, ids["waiting"], "20260101-work-02") == "claimed"


def test_a_run_claims_once(app_url: str, owner: psycopg.Connection, item: str) -> None:
    insert_stage_row(owner, OTHER, subject_ref="102")
    claim(app_url, item, "20260101-review-01")
    with pytest.raises(errors.CheckViolation, match="has claimed before"):
        claim(app_url, OTHER, "20260101-review-01")
    # Not after its claim ended either: the ID is never used again.
    release(app_url, item)
    with pytest.raises(errors.CheckViolation, match="has claimed before"):
        claim(app_url, item, "20260101-review-01")
    assert state(owner, OTHER) == (None, None, 0, 0, True)
    # Nor does one token serve two runs.
    with pytest.raises(errors.UniqueViolation), as_tier(app_url, DISPATCHER) as conn:
        conn.execute(
            CLAIM,
            (OTHER, "20260101-review-02", 30, sha("20260101-review-01"), None, "oslo", "normal"),
        )
    assert state(owner, OTHER) == (None, None, 0, 0, True)


GOOD = ("20260101-review-01", 30, "a" * 64, None, "lisbon", "normal")
BAD_SEATS = (
    None,
    "",
    "work/1",
    "review/urgent-1",
    "Lisbon",
    "lisbon1",
    "-lisbon",
    "lisbon-",
    "buenos--aires",
    "buenos aires",
    "a" * 41,
)
BAD_SEAT_TYPES = (None, "", "Normal", "urgent-1", "vip")


@pytest.mark.parametrize(
    "arguments",
    [
        *[(run, *GOOD[1:]) for run in (None, "", "-leading", "has space", "a:b", "x" * 101)],
        *[(GOOD[0], cap, *GOOD[2:]) for cap in (None, 0, -5, 1441)],
        *[
            (*GOOD[:2], token, *GOOD[3:])
            for token in (None, "", "abc", "A" * 64, "a" * 65, "g" * 64)
        ],
        *[(*GOOD[:3], current, *GOOD[4:]) for current in ("", "has space", "h\n", "x" * 129)],
        *[(*GOOD[:4], seat, GOOD[5]) for seat in BAD_SEATS],
        *[(*GOOD[:5], seat_type) for seat_type in BAD_SEAT_TYPES],
    ],
)
def test_a_claim_with_a_bad_value_is_refused(
    app_url: str, owner: psycopg.Connection, item: str, arguments: tuple[Any, ...]
) -> None:
    before = row_of(owner, item)
    with pytest.raises(errors.CheckViolation), as_tier(app_url, DISPATCHER) as conn:
        conn.execute(CLAIM, (item, *arguments))
    assert row_of(owner, item) == before
    assert owner.execute("SELECT count(*) FROM claims").fetchone() == (0,)


def test_an_urgent_seat_takes_an_urgent_item_only(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    with pytest.raises(errors.CheckViolation, match="seat lisbon is for an urgent item"):
        claim(app_url, item, "20260101-review-01", seat="lisbon", seat_type="urgent")
    assert state(owner, item) == (None, None, 0, 0, True)
    owner.execute("UPDATE items SET urgent = true WHERE id = %s", (item,))
    assert claim(app_url, item, "20260101-review-01", seat="lisbon", seat_type="urgent") == (
        "claimed"
    )
    rows = owner.execute("SELECT seat, seat_type FROM claims").fetchall()
    assert rows == [("lisbon", "urgent")]


def test_a_claim_s_seat_and_its_type_are_checked_in_the_table(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, "20260101-review-01", seat="buenos-aires")
    for change in (
        "UPDATE claims SET seat_type = 'vip'",
        "UPDATE claims SET seat_type = NULL",
        "UPDATE claims SET seat = NULL",
        "UPDATE claims SET seat = 'review/1'",
        "UPDATE claims SET seat = repeat('a', 41)",
    ):
        with pytest.raises(errors.CheckViolation), owner.transaction():
            owner.execute(change)
    assert owner.execute("SELECT seat, seat_type FROM claims").fetchall() == [
        ("buenos-aires", "normal")
    ]
    # A claim made before claims carried a seat has neither.
    owner.execute("UPDATE claims SET seat = NULL, seat_type = NULL")


def test_the_longest_and_the_shortest_cap(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    insert_stage_row(owner, OTHER, subject_ref="102")
    claim(app_url, item, "20260101-review-01", cap=1)
    claim(app_url, OTHER, "20260101-work-01", cap=1440)
    rows = owner.execute(
        "SELECT item_id, extract(epoch FROM expires_at - claimed_at)::int / 60 FROM claims "
        "ORDER BY item_id"
    ).fetchall()
    assert rows == [(item, 11), (OTHER, 1450)]


def test_two_claims_of_one_item_at_once_and_one_wins(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    together = threading.Barrier(2)

    def attempt(run: str) -> str:
        try:
            with as_tier(app_url, DISPATCHER) as conn:
                together.wait(timeout=10)
                row = conn.execute(
                    CLAIM, (item, run, 30, sha(run), None, seat_of(run), "normal")
                ).fetchone()
                # Keep the row locked for a while, so the other one waits for it.
                time.sleep(0.5)
        except psycopg.Error as exc:
            return exc.sqlstate or "?"
        assert row is not None
        return row[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["20260101-review-01", "20260101-review-02"]))
    assert sorted(outcomes) == ["BRD03", "claimed"]
    winner = ["20260101-review-01", "20260101-review-02"][outcomes.index("claimed")]
    assert state(owner, item)[0] == f"run:{winner}"
    assert claims_of(owner, item) == [(winner, None, None, None)]


# The staleness hash


def test_an_assess_item_on_an_issue_is_stamped_with_the_hash(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_stage_row(owner, ITEM, assignee="assess")
    with pytest.raises(errors.CheckViolation, match="claimed with the issue's hash"):
        claim(app_url, ITEM, "20260101-assess-01")
    assert state(owner, ITEM) == (None, None, 0, 0, True)
    assert claim(app_url, ITEM, "20260101-assess-01", current_hash="sha256:h2") == "claimed"
    assert owner.execute("SELECT input_hash FROM items").fetchall() == [("sha256:h2",)]


def test_an_assess_item_claimed_again_is_stamped_again(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_stage_row(owner, ITEM, assignee="assess")
    claim(app_url, ITEM, "20260101-assess-01", current_hash="h1")
    release(app_url, ITEM)
    claim(app_url, ITEM, "20260101-assess-02", current_hash="h2")
    assert owner.execute("SELECT input_hash FROM items").fetchall() == [("h2",)]


def test_an_assess_item_on_no_issue_needs_no_hash(app_url: str, owner: psycopg.Connection) -> None:
    insert_stage_row(
        owner,
        ITEM,
        assignee="assess",
        subject_type="alert",
        subject_repo=None,
        subject_ref="WidgetPressHot",
    )
    assert claim(app_url, ITEM, "20260101-assess-01") == "claimed"
    assert owner.execute("SELECT input_hash FROM items").fetchall() == [(None,)]


def test_a_work_item_assessed_under_another_hash_closes_as_stale(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_stage_row(owner, ITEM, input_hash="h1")
    assert claim(app_url, ITEM, "20260101-work-01", current_hash="h2") == "stale"
    row = owner.execute(
        "SELECT claimed_by, claim_expires, resolution, result, closed_at IS NOT NULL, input_hash "
        "FROM items WHERE id = %s",
        (ITEM,),
    ).fetchone()
    assert row == (None, None, "superseded", {"cause": "stale", "input_hash": "h2"}, True, "h1")
    assert owner.execute("SELECT count(*) FROM claims").fetchone() == (0,)
    assert token_owner(app_url, "20260101-work-01") is None
    # The run ID claimed nothing, so it is not used up.
    insert_stage_row(owner, OTHER, subject_ref="102")
    assert claim(app_url, OTHER, "20260101-work-01") == "claimed"


def test_a_work_item_under_the_same_hash_is_claimed(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_stage_row(owner, ITEM, input_hash="h1")
    with pytest.raises(errors.CheckViolation, match="claimed with the issue's hash"):
        claim(app_url, ITEM, "20260101-work-01")
    assert claim(app_url, ITEM, "20260101-work-01", current_hash="h1") == "claimed"
    assert state(owner, ITEM)[0] == "run:20260101-work-01"
    assert owner.execute("SELECT input_hash FROM items").fetchall() == [("h1",)]


@pytest.mark.parametrize("stage_name", ["work", "review", "deploy-check", "retro", "health"])
def test_an_item_that_carries_no_hash_takes_none(
    app_url: str, owner: psycopg.Connection, stage_name: str
) -> None:
    insert_stage_row(owner, ITEM, assignee=stage_name)
    with pytest.raises(errors.CheckViolation, match="only an assess item, or a work item"):
        claim(app_url, ITEM, "20260101-work-01", current_hash="h1")
    assert state(owner, ITEM) == (None, None, 0, 0, True)
    assert claim(app_url, ITEM, "20260101-work-01") == "claimed"
    assert owner.execute("SELECT input_hash FROM items").fetchall() == [(None,)]


# The queue


def test_the_queue_is_ranked_and_the_oldest_goes_first_within_a_rank(
    app_url: str, owner: psycopg.Connection
) -> None:
    refused_items(owner)
    # (id, stage, days old, urgent), written in no order.
    rows = [
        ("hhhhhhhh", "work", 9, False),
        ("jjjjjjjj", "retro", 30, False),
        ("kkkkkkkk", "assess", 5, False),
        ("mmmmmmmm", "work", 20, False),
        ("nnnnnnnn", "health", 1, False),
        ("pppppppp", "retro", 2, True),
        ("qqqqqqqq", "digest", 40, False),
        ("rrrrrrrr", "deploy-check", 3, False),
        ("ssssssss", "review", 4, False),
        ("tttttttt", "work", 6, True),
        ("uuuuuuuu", "assess", 7, False),
    ]
    for number, (item_id, stage_name, days, urgent) in enumerate(rows, start=401):
        insert_stage_row(
            owner, item_id, assignee=stage_name, subject_ref=str(number), urgent=urgent
        )
        owner.execute(
            "UPDATE items SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (days, item_id),
        )
    expected = [
        ("tttttttt", 1),
        ("pppppppp", 1),
        ("nnnnnnnn", 2),
        ("qqqqqqqq", 3),
        ("ssssssss", 4),
        ("rrrrrrrr", 5),
        ("uuuuuuuu", 6),
        ("kkkkkkkk", 6),
        ("mmmmmmmm", 7),
        ("hhhhhhhh", 7),
        ("jjjjjjjj", 8),
    ]
    for caller in (*EVERY_TIER, RECONCILER):
        with as_tier(app_url, caller) as conn:
            assert conn.execute(QUEUE).fetchall() == expected
    # A claimed item leaves the queue, and so does a closed one.
    claim(app_url, "tttttttt", "20260101-work-01")
    owner.execute(CLOSE, ("pppppppp",))
    assert queue(app_url) == [item_id for item_id, _ in expected[2:]]


# The release


def test_a_released_item_is_back_in_the_queue_at_once(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, "20260101-review-01")
    assert queue(app_url) == []
    assert release(app_url, item, "usage-limit") is True
    assert state(owner, item) == (None, None, 0, 1, True)
    rows = owner.execute(
        "SELECT run, ending, reason, ended_at IS NOT NULL, decision_id FROM claims"
    ).fetchall()
    assert rows == [("20260101-review-01", "released", "usage-limit", True, None)]
    assert token_owner(app_url, "20260101-review-01") is None
    assert queue(app_url) == [item]
    assert claim(app_url, item, "20260101-review-02") == "claimed"
    assert release(app_url, item, "stopped") is True
    assert state(owner, item) == (None, None, 0, 2, True)
    assert claims_of(owner, item) == [
        ("20260101-review-01", "released", "usage-limit", None),
        ("20260101-review-02", "released", "stopped", None),
    ]


def test_only_a_deploy_check_is_released_until_a_later_time(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    insert_stage_row(owner, OTHER, assignee="deploy-check", subject_type="pr", subject_ref="202")
    claim(app_url, item, "20260101-review-01")
    claim(app_url, OTHER, "20260101-deploy-check-01")
    with pytest.raises(errors.CheckViolation, match="only a deploy-check"):
        release(app_url, item, "not-live", at(owner, "1 hour"))
    for bad in ("-1 minute", "24 hours 5 minutes", "3 days"):
        with pytest.raises(errors.CheckViolation, match="at most a day ahead"):
            release(app_url, OTHER, "not-live", at(owner, bad))
    assert state(owner, OTHER)[0] == "run:20260101-deploy-check-01"
    assert state(owner, item)[0] == "run:20260101-review-01"

    when = at(owner, "23 hours")
    assert release(app_url, OTHER, "not-live", when) is True
    assert state(owner, OTHER) == (None, None, 0, 1, True)
    woken = owner.execute("SELECT not_before FROM items WHERE id = %s", (OTHER,)).fetchone()
    assert woken == (when,)
    # It is in the queue again at that time, not before.
    assert queue(app_url) == []
    owner.execute("UPDATE items SET not_before = now() WHERE id = %s", (OTHER,))
    assert queue(app_url) == [OTHER]


def test_a_release_with_no_time_keeps_the_item_s_date(
    app_url: str, owner: psycopg.Connection
) -> None:
    insert_stage_row(owner, ITEM, assignee="deploy-check", subject_type="pr", subject_ref="202")
    owner.execute("UPDATE items SET not_before = now() - interval '1 hour'")
    before = owner.execute("SELECT not_before FROM items").fetchone()
    claim(app_url, ITEM, "20260101-deploy-check-01")
    release(app_url, ITEM, "infra-window")
    assert owner.execute("SELECT not_before FROM items").fetchone() == before


def test_a_claim_that_ran_out_is_not_released(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, RUN_ID)
    lapse(owner, item)
    before = row_of(owner, item)
    for caller in (DISPATCHER, RUN):
        with pytest.raises(psycopg.Error) as refused:
            release(app_url, item, by=caller)
        assert refused.value.sqlstate == "BRD03"
        assert "has run out; it can only expire" in str(refused.value)
    assert row_of(owner, item) == before
    assert claims_of(owner, item) == [(RUN_ID, None, None, None)]


@pytest.mark.parametrize("reason", [None, "", "Stopped", "not live", "9lives", "x" * 41, "a_b"])
def test_a_release_needs_a_reason_that_is_a_short_name(
    app_url: str, owner: psycopg.Connection, item: str, reason: str | None
) -> None:
    claim(app_url, item, RUN_ID)
    with pytest.raises(errors.CheckViolation, match="a reason is a short lower-case name"):
        release(app_url, item, reason)  # pyright: ignore[reportArgumentType]
    assert state(owner, item)[3] == 0


def test_a_release_of_what_no_run_holds(app_url: str, owner: psycopg.Connection, item: str) -> None:
    assert release(app_url, "zzzzzzzz") is False
    insert_stage_row(owner, OTHER, claimed_by=SESSION.identity, subject_ref="102")
    for item_id, holder in ((item, "nobody"), (OTHER, SESSION.identity)):
        with pytest.raises(psycopg.Error) as refused:
            release(app_url, item_id)
        assert refused.value.sqlstate == "BRD03"
        assert f"item {item_id} is held by {holder}, not by a run" in str(refused.value)
    owner.execute(CLOSE, (item,))
    with pytest.raises(psycopg.Error) as refused:
        release(app_url, item)
    assert refused.value.sqlstate == "BRD01"
    assert state(owner, OTHER) == (SESSION.identity, None, 0, 0, True)


# Expiry


def test_only_a_claim_that_ran_out_expires(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    insert_stage_row(owner, OTHER, subject_ref="102")
    insert_stage_row(owner, "cccccccc", claimed_by=SESSION.identity, subject_ref="103")
    claim(app_url, item, "20260101-review-01")
    claim(app_url, OTHER, "20260101-work-01")
    assert expire(app_url) == []
    lapse(owner, item)
    running = row_of(owner, OTHER)
    session_s = row_of(owner, "cccccccc")
    assert expire(app_url) == [(item, "20260101-review-01", None)]
    assert state(owner, item) == (None, None, 1, 0, True)
    rows = owner.execute(
        "SELECT run, ending, ended_at IS NOT NULL, reason, decision_id FROM claims ORDER BY item_id"
    ).fetchall()
    assert rows == [
        ("20260101-review-01", "expired", True, None, None),
        ("20260101-work-01", None, False, None, None),
    ]
    assert row_of(owner, OTHER) == running
    assert row_of(owner, "cccccccc") == session_s
    # One expiry writes no decision, and the item is in the queue again.
    assert owner.execute("SELECT count(*) FROM items WHERE kind = 'decision'").fetchone() == (0,)
    assert owner.execute("SELECT count(*) FROM item_after").fetchone() == (0,)
    assert queue(app_url) == [item]
    # Counted once.
    assert expire(app_url) == []
    assert state(owner, item) == (None, None, 1, 0, True)


def test_a_session_s_claim_never_expires(app_url: str, owner: psycopg.Connection) -> None:
    """Only a run's claim has an end; should a session's item carry one, it is not counted."""
    insert_stage_row(owner, ITEM, claimed_by=SESSION.identity)
    owner.execute("UPDATE items SET claim_expires = now() - interval '1 hour'")
    before = row_of(owner, ITEM)
    assert expire(app_url) == []
    assert row_of(owner, ITEM) == before


SUBJECTS: dict[str, dict[str, Any]] = {
    "issue": {"subject_type": "issue", "subject_ref": "101"},
    "pr": {"subject_type": "pr", "subject_ref": "202"},
    "alert": {"subject_type": "alert", "subject_ref": "WidgetPressHot", "subject_repo": None},
    "key": {
        "subject_type": None,
        "subject_ref": None,
        "subject_repo": None,
        "key": "health:daily",
        "assignee": "health",
    },
}


@pytest.mark.parametrize("about", sorted(SUBJECTS))
@pytest.mark.parametrize("urgent", [False, True])
def test_two_expiries_in_a_row_write_one_decision_for_the_operator(
    app_url: str, owner: psycopg.Connection, about: str, urgent: bool
) -> None:
    insert_stage_row(owner, ITEM, urgent=urgent, **SUBJECTS[about])
    stage_name = SUBJECTS[about].get("assignee", "work")
    assert run_out(owner, app_url, ITEM, "20260101-work-01") is None
    assert claim(app_url, ITEM, "20260102-work-01") == "claimed"
    lapse(owner, ITEM)
    rows = expire(app_url, "human:olive")
    assert [(row[0], row[1]) for row in rows] == [(ITEM, "20260102-work-01")]
    decision = rows[0][2]
    assert decision is not None and decision != ITEM
    assert re.fullmatch(r"[a-hjkmnp-z2-9]{8}", decision)

    assert owner.execute("SELECT count(*) FROM items").fetchone() == (2,)
    row = owner.execute(
        "SELECT assignee, kind, claimed_by, created_by, created_run, operator_typed, urgent, "
        "subject_repo, subject_type, subject_ref, key, machine, holds, not_before, closed_at, "
        "fields FROM items WHERE id = %s",
        (decision,),
    ).fetchone()
    assert row is not None
    assert row[:7] == (
        "operator",
        "decision",
        "human:olive",
        "process:dispatcher",
        None,
        False,
        urgent,
    )
    wanted = {"subject_repo": REPO, **SUBJECTS[about]}
    if about == "key":
        # An item with only a key: its decision is about a finding, and has a key of its own.
        assert row[7:11] == (None, "finding", f"expired:{ITEM}", f"expired:{ITEM}")
    else:
        assert row[7:11] == (
            wanted["subject_repo"],
            wanted["subject_type"],
            wanted["subject_ref"],
            None,
        )
    assert row[11:15] == (None, True, None, None)
    fields = row[15]
    parsed = parse_operator_item(fields)
    assert isinstance(parsed, Decision)
    assert parsed.ticket == (Ticket(repo=REPO, number=101) if about == "issue" else None)
    assert len(parsed.options) == 2
    assert parsed.options[0].startswith("Try again")
    for named in (stage_name, ITEM, "20260101-work-01", "20260102-work-01"):
        assert named in parsed.summary

    # The item waits on its decision, so it is in no queue, and both claims name it.
    assert owner.execute("SELECT * FROM item_after").fetchall() == [(ITEM, decision)]
    assert state(owner, ITEM) == (None, None, 2, 0, True)
    assert queue(app_url) == []
    with pytest.raises(psycopg.Error) as refused:
        claim(app_url, ITEM, "20260103-work-01")
    assert refused.value.sqlstate == "BRD03"
    assert claims_of(owner, ITEM) == [
        ("20260101-work-01", "expired", None, decision),
        ("20260102-work-01", "expired", None, decision),
    ]
    assert expire(app_url) == []


def test_a_release_between_two_expiries_neither_counts_nor_starts_the_row_again(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    # A release, then one expiry: one expiry is counted, and no decision is written.
    claim(app_url, item, "20260101-review-01")
    release(app_url, item)
    assert run_out(owner, app_url, item, "20260101-review-02") is None
    # Another release, then the second expiry: the release did not start the row again.
    claim(app_url, item, "20260101-review-03")
    release(app_url, item, "infra-window")
    decision = run_out(owner, app_url, item, "20260101-review-04")
    assert decision is not None
    assert state(owner, item) == (None, None, 2, 2, True)
    assert claims_of(owner, item) == [
        ("20260101-review-01", "released", "stopped", None),
        ("20260101-review-02", "expired", None, decision),
        ("20260101-review-03", "released", "infra-window", None),
        ("20260101-review-04", "expired", None, decision),
    ]


def test_two_items_that_each_expired_once_get_no_decision(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    insert_stage_row(owner, OTHER, subject_ref="102")
    claim(app_url, item, "20260101-review-01")
    claim(app_url, OTHER, "20260101-work-01")
    lapse(owner, item)
    lapse(owner, OTHER)
    assert sorted(expire(app_url)) == [
        (item, "20260101-review-01", None),
        (OTHER, "20260101-work-01", None),
    ]
    assert owner.execute("SELECT count(*) FROM items").fetchone() == (2,)


@pytest.mark.parametrize(
    "holder", [None, "", "operator", "human:", "process:nightly", "session:0a1b2c3d", "human:a b"]
)
def test_the_decision_is_held_by_a_person_or_nothing_is_counted(
    app_url: str, owner: psycopg.Connection, item: str, holder: str | None
) -> None:
    claim(app_url, item, "20260101-review-01")
    lapse(owner, item)
    before = row_of(owner, item)
    with pytest.raises(errors.CheckViolation, match="held by a person"):
        expire(app_url, holder)  # pyright: ignore[reportArgumentType]
    assert row_of(owner, item) == before
    assert claims_of(owner, item) == [("20260101-review-01", None, None, None)]


# The decision's close


@pytest.fixture
def decided(app_url: str, owner: psycopg.Connection, item: str) -> str:
    """The decision item written after `item` ran out twice."""
    assert run_out(owner, app_url, item, "20260101-review-01") is None
    decision = run_out(owner, app_url, item, "20260101-review-02")
    assert decision is not None
    return decision


def close_as(app_url: str, item_id: str, resolution: str, result: dict[str, Any] | None) -> None:
    """Close as the operator, the way the store's verbs do."""
    with as_tier(app_url, OPERATOR) as conn:
        closed = conn.execute(
            CLOSE_AS, (resolution, None if result is None else Jsonb(result), item_id)
        )
        assert closed.rowcount == 1


def test_try_again_lets_the_item_go_for_two_more_runs(
    app_url: str, owner: psycopg.Connection, item: str, decided: str
) -> None:
    close_as(app_url, decided, "answered", {"option": 1, "answer": "Try again"})
    assert state(owner, item) == (None, None, 2, 0, True)
    assert queue(app_url) == [item]
    # One more expiry is the first of a new row; the second writes the second decision.
    assert run_out(owner, app_url, item, "20260101-review-03") is None
    assert queue(app_url) == [item]
    second = run_out(owner, app_url, item, "20260101-review-04")
    assert second is not None and second != decided
    assert state(owner, item) == (None, None, 4, 0, True)
    assert queue(app_url) == []
    assert claims_of(owner, item) == [
        ("20260101-review-01", "expired", None, decided),
        ("20260101-review-02", "expired", None, decided),
        ("20260101-review-03", "expired", None, second),
        ("20260101-review-04", "expired", None, second),
    ]
    assert set(owner.execute("SELECT * FROM item_after").fetchall()) == {
        (item, decided),
        (item, second),
    }
    # The second one stops it like the first would have.
    close_as(app_url, second, "answered", {"option": 2, "answer": "Stop"})
    assert state(owner, item)[4] is False


@pytest.mark.parametrize(
    "result",
    [
        {"option": 2, "answer": "Stop: close the item as superseded"},
        {"answer": "sell the press"},
        {"option": 3, "answer": "there is no third"},
        None,
    ],
    ids=["option-2", "own-text", "option-3", "no-result"],
)
def test_any_other_answer_closes_the_item_as_superseded(
    app_url: str, owner: psycopg.Connection, item: str, decided: str, result: dict[str, Any] | None
) -> None:
    close_as(app_url, decided, "answered", result)
    row = owner.execute(
        "SELECT resolution, result, closed_at = (SELECT closed_at FROM items WHERE id = %s) "
        "FROM items WHERE id = %s",
        (decided, item),
    ).fetchone()
    # The cascade's result and the decision's own moment, so the same reading finds both.
    assert row == (
        "superseded",
        {"cause": "after", "item": decided, "resolution": "answered"},
        True,
    )
    assert queue(app_url) == []


@pytest.mark.parametrize("resolution", ["rejected", "replied"])
def test_a_reject_or_a_reply_closes_the_item_through_the_cascade(
    app_url: str, owner: psycopg.Connection, item: str, decided: str, resolution: str
) -> None:
    close_as(app_url, decided, resolution, None)
    row = owner.execute("SELECT resolution, result FROM items WHERE id = %s", (item,)).fetchone()
    assert row == ("superseded", {"cause": "after", "item": decided, "resolution": resolution})


def test_a_decision_of_another_kind_closes_nothing_when_answered(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    """Only the item whose claims name the decision closes with it."""
    insert_row(owner, id=OTHER, kind="decision", subject_ref="102")
    claim(app_url, item, "20260101-review-01")
    close_as(app_url, OTHER, "answered", {"option": 2, "answer": "leave it"})
    assert state(owner, item)[4] is True
    assert claims_of(owner, item) == [("20260101-review-01", None, None, None)]


# A refused result: a failed run, like an expiry


def refused(app_url: str, item_id: str, run: str, holder: str | None = None) -> None:
    """One claim whose run left no result that could be accepted, released as such."""
    assert claim(app_url, item_id, run) == "claimed"
    assert release(app_url, item_id, "result-refused", holder=holder) is True


def decision_of(owner: psycopg.Connection, item_id: str) -> str | None:
    """The decision the item's claims name, if any; one at most."""
    rows = owner.execute(
        "SELECT DISTINCT decision_id FROM claims WHERE item_id = %s AND decision_id IS NOT NULL",
        (item_id,),
    ).fetchall()
    assert len(rows) <= 1
    return rows[0][0] if rows else None


@pytest.mark.parametrize(
    "failures",
    [("expired", "refused"), ("refused", "expired"), ("refused", "refused")],
)
def test_two_failed_runs_in_a_row_expired_or_refused_write_the_decision(
    app_url: str, owner: psycopg.Connection, failures: tuple[str, str]
) -> None:
    insert_stage_row(owner, ITEM, urgent=True)
    runs = ("20260101-work-01", "20260102-work-01")
    told = {"expired": "ran out of time", "refused": "left no result that could be accepted"}

    def fail(how: str, run: str) -> str | None:
        if how == "expired":
            return run_out(owner, app_url, ITEM, run)
        refused(app_url, ITEM, run)
        return decision_of(owner, ITEM)

    # The first failed run writes nothing, and the item is in the queue again at once.
    assert fail(failures[0], runs[0]) is None
    assert owner.execute("SELECT count(*) FROM items").fetchone() == (1,)
    assert queue(app_url) == [ITEM]

    decision = fail(failures[1], runs[1])
    assert decision is not None
    assert decision_of(owner, ITEM) == decision
    row = owner.execute(
        "SELECT assignee, kind, claimed_by, created_by, urgent, subject_ref, fields "
        "FROM items WHERE id = %s",
        (decision,),
    ).fetchone()
    assert row is not None
    assert row[:6] == ("operator", "decision", OPERATOR.identity, DISPATCHER.identity, True, "101")
    parsed = parse_operator_item(row[6])
    assert isinstance(parsed, Decision)
    assert parsed.ticket == Ticket(repo=REPO, number=101)
    assert parsed.options[0].startswith("Try again")
    assert f"The work item {ITEM} failed twice in a row (" in parsed.summary
    first, second = (f"run {run} {told[how]}" for run, how in zip(runs, failures, strict=True))
    assert f"({first}, then {second})" in parsed.summary
    if "refused" in failures:
        assert "ran out of time twice" not in parsed.summary

    # The item waits on it, and both claims name it. A refused result is a release: it
    # counts in `releases`, not in `expiries`.
    assert owner.execute("SELECT * FROM item_after").fetchall() == [(ITEM, decision)]
    assert state(owner, ITEM) == (
        None,
        None,
        failures.count("expired"),
        failures.count("refused"),
        True,
    )
    assert queue(app_url) == []
    reasons = {"expired": ("expired", None), "refused": ("released", "result-refused")}
    assert claims_of(owner, ITEM) == [
        (run, *reasons[how], decision) for run, how in zip(runs, failures, strict=True)
    ]
    assert expire(app_url) == []


def test_another_release_between_two_refused_results_neither_counts_nor_starts_the_row_again(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    refused(app_url, item, "20260101-review-01")
    claim(app_url, item, "20260101-review-02")
    release(app_url, item, "stopped")
    assert decision_of(owner, item) is None
    refused(app_url, item, "20260101-review-03")
    decision = decision_of(owner, item)
    assert decision is not None
    assert state(owner, item) == (None, None, 0, 3, True)
    assert claims_of(owner, item) == [
        ("20260101-review-01", "released", "result-refused", decision),
        ("20260101-review-02", "released", "stopped", None),
        ("20260101-review-03", "released", "result-refused", decision),
    ]


@pytest.mark.parametrize("reason", ["start-failed", "infra-window", "usage-limit", "stopped"])
def test_no_other_reason_counts_toward_the_decision(
    app_url: str, owner: psycopg.Connection, item: str, reason: str
) -> None:
    """An expiry, then a release for this reason, twice over: still no decision."""
    assert run_out(owner, app_url, item, "20260101-review-01") is None
    claim(app_url, item, "20260101-review-02")
    release(app_url, item, reason)
    claim(app_url, item, "20260101-review-03")
    release(app_url, item, reason)
    assert owner.execute("SELECT count(*) FROM items WHERE kind = 'decision'").fetchone() == (0,)
    assert decision_of(owner, item) is None
    assert queue(app_url) == [item]


def test_a_decision_after_a_refused_result_closes_the_item_on_stop(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    assert run_out(owner, app_url, item, "20260101-review-01") is None
    refused(app_url, item, "20260101-review-02")
    decision = decision_of(owner, item)
    assert decision is not None
    close_as(app_url, decision, "answered", {"option": 2, "answer": "Stop"})
    row = owner.execute("SELECT resolution, result FROM items WHERE id = %s", (item,)).fetchone()
    assert row == ("superseded", {"cause": "after", "item": decision, "resolution": "answered"})


@pytest.mark.parametrize(
    "holder", [None, "", "operator", "human:", "process:dispatcher", "human:a b"]
)
def test_a_refused_result_names_a_person_for_the_decision_or_nothing_is_released(
    app_url: str, owner: psycopg.Connection, item: str, holder: str | None
) -> None:
    claim(app_url, item, "20260101-review-01")
    before = row_of(owner, item)
    with (
        pytest.raises(errors.CheckViolation, match="held by a person"),
        as_tier(app_url, DISPATCHER) as conn,
    ):
        conn.execute(RELEASE, (item, "result-refused", None, holder))
    assert row_of(owner, item) == before
    assert claims_of(owner, item) == [("20260101-review-01", None, None, None)]
    if holder is not None:
        # A holder given with another reason is checked too.
        with pytest.raises(errors.CheckViolation, match="held by a person"):
            release(app_url, item, "stopped", holder=holder)
        assert row_of(owner, item) == before


def test_only_a_failed_run_s_claim_names_a_decision(owner: psycopg.Connection, item: str) -> None:
    insert_row(owner, id=OTHER, kind="decision", subject_ref="102")
    for run, reason in (("20260101-review-01", "stopped"), ("20260101-review-02", None)):
        owner.execute(
            "INSERT INTO claims (run, item_id, token_sha256, expires_at, ended_at, ending, reason) "
            "VALUES (%s, %s, %s, now(), now(), %s, %s)",
            (run, item, sha(run), "released" if reason else "closed", reason),
        )
        with pytest.raises(errors.CheckViolation, match="claims_decision_on_failed_run"):
            owner.execute("UPDATE claims SET decision_id = %s WHERE run = %s", (OTHER, run))


# A close under a claim


def test_a_run_closes_its_item_and_that_ends_the_claim(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, RUN_ID)
    with as_tier(app_url, OTHER_RUN) as conn:
        assert conn.execute(CLOSE, (item,)).rowcount == 0
    with as_tier(app_url, RUN) as conn:
        assert conn.execute(CLOSE, (item,)).rowcount == 1
    rows = owner.execute(
        "SELECT c.run, c.ending, c.reason, c.ended_at = i.closed_at, i.claimed_by, i.resolution "
        "FROM claims AS c JOIN items AS i ON i.id = c.item_id"
    ).fetchall()
    # The item keeps its holder: it says who closed it.
    assert rows == [(RUN_ID, "closed", None, True, RUN.identity, "done")]
    assert expire(app_url) == []


@pytest.mark.parametrize("resolution", ["rejected", "superseded"])
def test_the_operator_s_close_of_a_claimed_item_ends_the_claim(
    app_url: str, owner: psycopg.Connection, item: str, resolution: str
) -> None:
    claim(app_url, item, RUN_ID)
    close_as(app_url, item, resolution, {"reason": "the press is sold"})
    assert claims_of(owner, item) == [(RUN_ID, "closed", None, None)]
    assert token_owner(app_url, RUN_ID) is None
    # A closed item's claim does not expire later.
    owner.execute("UPDATE claims SET expires_at = now() - interval '1 minute'")
    assert expire(app_url) == []
    assert claims_of(owner, item) == [(RUN_ID, "closed", None, None)]


def test_a_cascade_that_closes_a_claimed_item_ends_the_claim(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    insert_row(owner, id=OTHER, subject_ref="102")
    owner.execute("INSERT INTO item_after (item_id, after_id) VALUES (%s, %s)", (item, OTHER))
    # Claimed before the edge was there, as the board's own edge is written.
    owner.execute(
        "UPDATE items SET claimed_by = %s, claim_expires = now() + interval '1 hour' WHERE id = %s",
        (RUN.identity, item),
    )
    owner.execute(
        "INSERT INTO claims (run, item_id, token_sha256, expires_at) "
        "VALUES (%s, %s, %s, now() + interval '1 hour')",
        (RUN_ID, item, sha(RUN_ID)),
    )
    close_as(app_url, OTHER, "rejected", None)
    assert claims_of(owner, item) == [(RUN_ID, "closed", None, None)]


def test_a_close_with_no_claim_ends_none(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    insert_stage_row(owner, OTHER, subject_ref="102")
    claim(app_url, OTHER, "20260101-work-01")
    close_as(app_url, item, "superseded", None)
    assert claims_of(owner, OTHER) == [("20260101-work-01", None, None, None)]


# Whose token


def test_a_token_names_its_run_while_the_claim_runs(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    insert_stage_row(owner, OTHER, assignee="assess", subject_ref="102")
    claim(app_url, item, "20260101-review-01")
    claim(app_url, OTHER, "20260101-assess-01", current_hash="h1")
    assert token_owner(app_url, "20260101-review-01") == ("20260101-review-01", item, "review")
    assert token_owner(app_url, "20260101-assess-01") == ("20260101-assess-01", OTHER, "assess")
    assert token_owner(app_url, "no-such-token") is None
    with psycopg.connect(app_url, autocommit=True) as conn:
        # The hash is asked for, not the token.
        assert conn.execute(TOKEN, ("20260101-review-01",)).fetchone() is None
        assert conn.execute(TOKEN, (None,)).fetchone() is None


def test_a_token_is_dead_once_the_item_closes(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, RUN_ID)
    with as_tier(app_url, RUN) as conn:
        assert conn.execute(CLOSE, (item,)).rowcount == 1
    assert token_owner(app_url, RUN_ID) is None


def test_a_token_is_dead_once_the_claim_is_released(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, "20260101-review-01")
    release(app_url, item)
    assert token_owner(app_url, "20260101-review-01") is None
    # The next run's token is its own.
    claim(app_url, item, "20260101-review-02")
    assert token_owner(app_url, "20260101-review-01") is None
    assert token_owner(app_url, "20260101-review-02") == ("20260101-review-02", item, "review")


def test_a_token_is_dead_once_the_claim_runs_out_counted_or_not(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, "20260101-review-01")
    lapse(owner, item)
    assert claims_of(owner, item) == [("20260101-review-01", None, None, None)]
    assert token_owner(app_url, "20260101-review-01") is None
    expire(app_url)
    assert token_owner(app_url, "20260101-review-01") is None


# A run whose claim ran out


def test_a_run_whose_claim_ran_out_closes_nothing(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, RUN_ID)
    touch = 'UPDATE items SET result = \'{"note": "seen"}\' WHERE id = %s'
    with as_tier(app_url, RUN) as conn:
        assert conn.execute(touch, (item,)).rowcount == 1
    lapse(owner, item)
    before = row_of(owner, item)
    give_up = "UPDATE items SET closed_at = now(), resolution = 'superseded' WHERE id = %s"
    for statement in (CLOSE, touch, give_up):
        with as_tier(app_url, RUN) as conn:
            assert conn.execute(statement, (item,)).rowcount == 0
    assert row_of(owner, item) == before
    # The operator still may.
    close_as(app_url, item, "rejected", None)


def test_a_run_closes_its_item_until_the_claim_runs_out(
    app_url: str, owner: psycopg.Connection, item: str
) -> None:
    claim(app_url, item, RUN_ID, cap=1)
    owner.execute("UPDATE items SET claim_expires = now() + interval '5 seconds'")
    with as_tier(app_url, RUN) as conn:
        assert conn.execute(CLOSE, (item,)).rowcount == 1


def test_an_agent_s_item_with_no_end_is_closed_as_before(
    app_url: str, owner: psycopg.Connection
) -> None:
    """A session's claim has no expiry, and the new policy leaves it alone."""
    insert_stage_row(owner, ITEM, claimed_by=SESSION.identity)
    with as_tier(app_url, SESSION) as conn:
        assert conn.execute(CLOSE, (ITEM,)).rowcount == 1
    policies = owner.execute(
        "SELECT policyname, permissive, roles::text, cmd FROM pg_policies "
        "WHERE tablename = 'items' AND permissive = 'RESTRICTIVE' AND cmd = 'UPDATE'"
    ).fetchall()
    assert policies == [("items_update_within_claim", "RESTRICTIVE", "{board_agent}", "UPDATE")]
