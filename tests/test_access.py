"""The roles and the policies, with plain SQL past the store.

The store is not trusted here: these tests show what Postgres itself lets each tier do.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

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
    as_tier,
    insert_row,
    insert_stage_row,
    row_of,
)
from psycopg import errors
from psycopg.types.json import Jsonb

from factory_board.store import Caller

TIERS = [OPERATOR, SESSION, RECONCILER]
CLOSE = "UPDATE items SET closed_at = now(), resolution = 'done' WHERE id = %s"
CLOSE_AS = "UPDATE items SET closed_at = now(), resolution = %s WHERE id = %s"


@pytest.fixture
def owner(migrated: None, owner_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(owner_url, autocommit=True) as connection:
        yield connection


@pytest.fixture
def held(owner: psycopg.Connection) -> dict[str, str]:
    """One open item per kind of holder; the key is who holds it.

    The operator's items are held by a person or a process. The others are stage items.
    """
    ids = {
        "human:operator": "hhhhhhhh",
        "process:reconciler": "pppppppp",
        "process:nightly": "nnnnnnnn",
    }
    for number, (holder, item_id) in enumerate(ids.items(), start=101):
        insert_row(owner, id=item_id, claimed_by=holder, subject_ref=str(number))
    stage_ids = {
        "session:0a1b2c3d": "ssssssss",
        "session:9z8y7x6w": "tttttttt",
        "run:20260101-work-01": "rrrrrrrr",
        "nobody": "uuuuuuuu",
    }
    for number, (holder, item_id) in enumerate(stage_ids.items(), start=201):
        insert_stage_row(
            owner,
            item_id,
            claimed_by=None if holder == "nobody" else holder,
            subject_ref=str(number),
        )
    return {**ids, **stage_ids}


def test_the_app_login_can_do_nothing_until_it_takes_a_tier(migrated: None, app_url: str) -> None:
    with psycopg.connect(app_url, autocommit=True) as conn:
        assert conn.execute("SELECT session_user, current_user").fetchone() == (
            "board_app",
            "board_app",
        )
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM items")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM replies")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM item_after")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT set_pr_head_sha('hhhhhhhh', 'example/repo', '202', %s)", (SHA_A,))


def test_the_app_login_is_not_the_owner_and_cannot_become_it(
    owner: psycopg.Connection, app_url: str
) -> None:
    row = owner.execute(
        "SELECT rolsuper, rolbypassrls, rolinherit, rolcreaterole FROM pg_roles "
        "WHERE rolname = 'board_app'"
    ).fetchone()
    assert row == (False, False, False, False)
    members = owner.execute(
        "SELECT pg_get_userbyid(roleid) FROM pg_auth_members "
        "WHERE member = 'board_app'::regrole ORDER BY 1"
    ).fetchall()
    assert members == [("board_agent",), ("board_operator",), ("board_process",)]
    with psycopg.connect(app_url, autocommit=True) as conn:
        for role in ("board_owner", "postgres"):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(f"SET ROLE {role}")


@pytest.mark.parametrize("caller", TIERS, ids=lambda c: c.tier.name)
def test_every_tier_reads_every_item(app_url: str, held: dict[str, str], caller: Caller) -> None:
    with as_tier(app_url, caller) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (caller.tier.value,)
        assert conn.execute("SELECT count(*) FROM items").fetchone() == (len(held),)


@pytest.mark.parametrize(
    ("caller", "holder", "allowed"),
    [
        (OPERATOR, "human:operator", True),
        (OPERATOR, "process:reconciler", True),
        (OPERATOR, "session:0a1b2c3d", True),
        (OPERATOR, "run:20260101-work-01", True),
        (OPERATOR, "nobody", True),
        # An agent: only the item it holds itself.
        (SESSION, "session:0a1b2c3d", True),
        (SESSION, "session:9z8y7x6w", False),
        (SESSION, "run:20260101-work-01", False),
        (SESSION, "nobody", False),
        (SESSION, "human:operator", False),
        (SESSION, "process:reconciler", False),
        (OTHER_SESSION, "session:0a1b2c3d", False),
        (OTHER_SESSION, "session:9z8y7x6w", True),
        (RUN, "run:20260101-work-01", True),
        (RUN, "session:0a1b2c3d", False),
        (RUN, "nobody", False),
        (RUN, "human:operator", False),
        (RUN, "process:nightly", False),
        # A process: likewise.
        (RECONCILER, "process:reconciler", True),
        (RECONCILER, "process:nightly", False),
        (RECONCILER, "human:operator", False),
        (RECONCILER, "session:0a1b2c3d", False),
        # The reconciler closes a stage item nobody holds; no other process does.
        (RECONCILER, "nobody", True),
        (NIGHTLY, "nobody", False),
        (NIGHTLY, "process:nightly", True),
        (NIGHTLY, "process:reconciler", False),
    ],
    ids=lambda v: v.identity if isinstance(v, Caller) else str(v),
)
def test_who_may_update_whose_item(
    app_url: str,
    owner: psycopg.Connection,
    held: dict[str, str],
    caller: Caller,
    holder: str,
    allowed: bool,
) -> None:
    """A refused UPDATE raises nothing: it changes no row."""
    item_id = held[holder]
    before = row_of(owner, item_id)
    with as_tier(app_url, caller) as conn:
        changed = conn.execute(CLOSE, (item_id,)).rowcount
    assert changed == (1 if allowed else 0)
    if allowed:
        assert row_of(owner, item_id) != before
    else:
        assert row_of(owner, item_id) == before


def test_set_local_role_picks_the_policy_on_one_connection(
    app_url: str, owner: psycopg.Connection, held: dict[str, str]
) -> None:
    """The same statement on the same connection: the role in force decides, and it ends
    with its transaction."""
    item_id = held["human:operator"]
    bump = 'UPDATE items SET result = \'{"note": "seen"}\' WHERE id = %s'
    with psycopg.connect(app_url, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE board_agent")
            assert conn.execute(bump, (item_id,)).rowcount == 0
        assert conn.execute("SELECT current_user").fetchone() == ("board_app",)
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(bump, (item_id,))
        with conn.transaction():
            conn.execute("SET LOCAL ROLE board_process")
            conn.execute("SELECT set_config('board.actor', 'process:reconciler', true)")
            assert conn.execute(bump, (item_id,)).rowcount == 0
        assert conn.execute("SELECT current_setting('board.actor', true)").fetchone() in [
            ("",),
            (None,),
        ]
        with conn.transaction():
            conn.execute("SET LOCAL ROLE board_operator")
            assert conn.execute(bump, (item_id,)).rowcount == 1
        assert conn.execute("SELECT current_user").fetchone() == ("board_app",)
    assert owner.execute("SELECT result FROM items WHERE id = %s", (item_id,)).fetchone() == (
        {"note": "seen"},
    )


def test_a_process_with_no_actor_set_updates_nothing(app_url: str, held: dict[str, str]) -> None:
    with psycopg.connect(app_url) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE board_process")
        assert conn.execute("UPDATE items SET result = '{}'").rowcount == 0


def test_an_agent_whose_actor_claims_a_person_s_identity_still_updates_nothing(
    app_url: str, held: dict[str, str]
) -> None:
    """The second guard: should board.actor ever be wrong, the prefix test still holds."""
    for actor in ("human:operator", "process:reconciler", ""):
        with psycopg.connect(app_url) as conn, conn.transaction():
            conn.execute("SET LOCAL ROLE board_agent")
            conn.execute("SELECT set_config('board.actor', %s, true)", (actor,))
            assert conn.execute("UPDATE items SET result = '{}'").rowcount == 0


@pytest.mark.parametrize("caller", TIERS, ids=lambda c: c.tier.name)
def test_no_tier_moves_a_claim(app_url: str, held: dict[str, str], caller: Caller) -> None:
    """Claiming and handing over come with a later build step, and with their own grant."""
    for assignment in (
        "claimed_by = 'human:operator'",
        "claimed_by = NULL",
        "claim_expires = now()",
    ):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(f"UPDATE items SET {assignment} WHERE id = %s", (held["nobody"],))  # pyright: ignore[reportArgumentType]


def test_an_upsert_onto_a_refused_row_raises(app_url: str, held: dict[str, str]) -> None:
    """Unlike a plain UPDATE, ON CONFLICT DO UPDATE is not silent, so the store never uses it."""
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, SESSION) as conn:
        conn.execute(
            "INSERT INTO items (id, assignee, subject_repo, subject_type, subject_ref, kind, "
            "claimed_by, created_by, created_run) VALUES (%s, 'operator', 'example/repo', "
            "'issue', '101', 'manual', 'human:operator', %s, '0a1b2c3d') "
            "ON CONFLICT (id) DO UPDATE SET result = '{}'",
            (held["human:operator"], SESSION.identity),
        )


def run_of(caller: Caller) -> str | None:
    """What created_run must be for this caller: an agent's own run or session ID."""
    return caller.identity.partition(":")[2] if caller.tier.name == "AGENT" else None


@pytest.mark.parametrize("caller", [*TIERS, RUN], ids=lambda c: c.identity)
def test_every_tier_inserts_under_its_own_identity_only(
    app_url: str, migrated: None, caller: Caller
) -> None:
    with as_tier(app_url, caller) as conn:
        # An item for the operator, held by the operator: any tier may write one.
        insert_row(
            conn,
            created_by=caller.identity,
            created_run=run_of(caller),
            claimed_by="human:operator",
        )
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        insert_row(
            conn, id="bbbbbbbb", created_by="human:somebody-else", created_run=run_of(caller)
        )


@pytest.mark.parametrize("caller", [SESSION, RUN], ids=lambda c: c.identity)
@pytest.mark.parametrize("created_run", [None, "9z8y7x6w", "", "20260101-work-02"])
def test_an_agent_cannot_attribute_an_item_to_another_run(
    app_url: str, migrated: None, caller: Caller, created_run: str | None
) -> None:
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        insert_row(conn, created_by=caller.identity, created_run=created_run)


@pytest.mark.parametrize("caller", [SESSION, RUN, RECONCILER], ids=lambda c: c.identity)
@pytest.mark.parametrize("holder", [None, "session:0a1b2c3d", "run:20260101-work-01"])
def test_an_item_for_the_operator_is_never_held_by_an_agent_or_nobody(
    app_url: str, migrated: None, caller: Caller, holder: str | None
) -> None:
    """Else its writer could change it after the operator has read it."""
    with pytest.raises(errors.CheckViolation) as refused, as_tier(app_url, caller) as conn:
        insert_row(conn, created_by=caller.identity, created_run=run_of(caller), claimed_by=holder)
    assert refused.value.diag.constraint_name == "items_operator_item_holder"


@pytest.mark.parametrize("caller", TIERS, ids=lambda c: c.tier.name)
@pytest.mark.parametrize(
    "forged",
    [
        {"closed_at": "2030-01-01", "resolution": "answered"},
        {"closed_at": "2030-01-01", "resolution": "done", "result": Jsonb({"answer": "yes"})},
        {"result": Jsonb({"answer": "yes"})},
        {"expiries": 1},
        {"releases": 2},
        {"claim_expires": "2030-01-01"},
        {"head_sha_moves": Jsonb([{"from": SHA_A, "to": SHA_B, "by": "human:operator"}])},
        {"created_at": "2020-01-01"},
    ],
    ids=lambda forged: "+".join(forged),
)
def test_no_tier_inserts_an_item_with_a_past(
    app_url: str, owner: psycopg.Connection, caller: Caller, forged: dict[str, Any]
) -> None:
    """A new item is open, with no result, no counted claims and no moves."""
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        insert_row(conn, created_by=caller.identity, created_run=run_of(caller), **forged)
    assert owner.execute("SELECT count(*) FROM items").fetchone() == (0,)


def test_a_process_cannot_say_the_operator_typed(app_url: str, migrated: None) -> None:
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, RECONCILER) as conn:
        insert_row(conn, created_by=RECONCILER.identity, operator_typed=True)
    with as_tier(app_url, RECONCILER) as conn:
        insert_row(conn, created_by=RECONCILER.identity, operator_typed=False)
    # An agent's word is taken for it, as its session ID is.
    with as_tier(app_url, SESSION) as conn:
        insert_row(
            conn,
            id="bbbbbbbb",
            created_by=SESSION.identity,
            created_run="0a1b2c3d",
            operator_typed=True,
        )


def test_the_insert_policies_hold_without_the_column_grants(owner: psycopg.Connection) -> None:
    """The policies refuse a forged row by themselves: each names every forgeable column."""
    rows = owner.execute(
        "SELECT policyname, with_check FROM pg_policies "
        "WHERE tablename = 'items' AND cmd = 'INSERT' ORDER BY policyname"
    ).fetchall()
    assert [name for name, _ in rows] == [
        "items_insert_agent",
        "items_insert_operator",
        "items_insert_process",
    ]
    for _, check in rows:
        for column in (
            "closed_at",
            "resolution",
            "result",
            "expiries",
            "releases",
            "head_sha_moves",
        ):
            assert column in check


@pytest.mark.parametrize("caller", TIERS, ids=lambda c: c.tier.name)
def test_no_tier_deletes(app_url: str, held: dict[str, str], caller: Caller) -> None:
    for statement in (
        "DELETE FROM items",
        "TRUNCATE items",
        "DELETE FROM replies",
        "DELETE FROM item_after",
        "TRUNCATE item_after",
    ):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(statement)


@pytest.mark.parametrize("caller", [*TIERS, RUN, NIGHTLY], ids=lambda c: c.identity)
@pytest.mark.parametrize(
    "assignment",
    [
        "id = 'zzzzzzzz'",
        "assignee = 'review'",
        "kind = 'review'",
        "subject_ref = '999'",
        "subject_repo = 'example/other'",
        "key = 'other'",
        "machine = 'pilot'",
        "created_by = 'human:operator'",
        "created_run = 'other'",
        "created_at = now()",
        "operator_typed = true",
        'fields = \'{"command": "make gadgets"}\'',
        "fields = jsonb_set(fields, '{head_sha}', '\"cafe\"')",
        "head_sha_moves = '[]'",
        "expiries = 0",
        "releases = 0",
        "reasons = '{}'",
        "input_hash = 'h'",
        "holds = false",
        "holds = true",
    ],
)
def test_no_tier_rewrites_what_an_item_is_or_says(
    app_url: str, held: dict[str, str], caller: Caller, assignment: str
) -> None:
    """Not even on the row the tier may otherwise update: its own."""
    item_id = held["human:operator" if caller is OPERATOR else caller.identity]
    with as_tier(app_url, caller) as conn:
        touched = conn.execute("UPDATE items SET result = '{}' WHERE id = %s", (item_id,))
        assert touched.rowcount == 1
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        conn.execute(f"UPDATE items SET {assignment} WHERE id = %s", (item_id,))  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("caller", [SESSION, RUN, RECONCILER], ids=lambda c: c.identity)
def test_deferring_and_bumping_are_the_operator_s(
    app_url: str, held: dict[str, str], caller: Caller
) -> None:
    for assignment in ("urgent = true", "not_before = now()"):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(f"UPDATE items SET {assignment} WHERE id = %s", (held[caller.identity],))  # pyright: ignore[reportArgumentType]
        with as_tier(app_url, OPERATOR) as conn:
            changed = conn.execute(
                f"UPDATE items SET {assignment} WHERE id = %s", (held[caller.identity],)
            )  # pyright: ignore[reportArgumentType]
            assert changed.rowcount == 1


def test_the_update_grants_are_these_and_no_more(owner: psycopg.Connection) -> None:
    rows = owner.execute(
        "SELECT grantee, privilege_type, string_agg(column_name, ',' ORDER BY column_name) "
        "FROM information_schema.column_privileges "
        "WHERE table_name = 'items' AND grantee LIKE 'board!_%' ESCAPE '!' "
        "AND grantee <> 'board_owner' AND privilege_type IN ('UPDATE', 'INSERT') "
        "GROUP BY grantee, privilege_type ORDER BY grantee, privilege_type"
    ).fetchall()
    inserted = (
        "assignee,claimed_by,created_by,created_run,fields,holds,id,input_hash,key,kind,"
        "machine,"
        "not_before,operator_typed,reasons,subject_ref,subject_repo,subject_type,urgent"
    )
    assert rows == [
        ("board_agent", "INSERT", inserted),
        ("board_agent", "UPDATE", "closed_at,resolution,result"),
        ("board_operator", "INSERT", inserted),
        ("board_operator", "UPDATE", "closed_at,not_before,resolution,result,urgent"),
        ("board_process", "INSERT", inserted),
        ("board_process", "UPDATE", "closed_at,resolution,result"),
    ]


def test_the_owner_may_delete(owner: psycopg.Connection, held: dict[str, str]) -> None:
    assert owner.execute("DELETE FROM items WHERE id = %s", (held["nobody"],)).rowcount == 1


@pytest.mark.parametrize("caller", [*TIERS, RUN], ids=lambda c: c.identity)
def test_a_closed_item_cannot_be_changed(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], caller: Caller
) -> None:
    # The caller's own item: the policies would let every one of these through.
    item_id = held["human:operator" if caller is OPERATOR else caller.identity]
    owner.execute(CLOSE, (item_id,))
    before = row_of(owner, item_id)
    statements = [
        "UPDATE items SET closed_at = NULL, resolution = NULL WHERE id = %s",
        "UPDATE items SET resolution = 'rejected' WHERE id = %s",
        "UPDATE items SET closed_at = now() WHERE id = %s",
        "UPDATE items SET result = '{}' WHERE id = %s",
    ]
    if caller is OPERATOR:
        statements += [
            "UPDATE items SET urgent = true WHERE id = %s",
            "UPDATE items SET not_before = now() WHERE id = %s",
        ]
    for statement in statements:
        with pytest.raises(psycopg.Error) as refused, as_tier(app_url, caller) as conn:
            conn.execute(statement, (item_id,))  # pyright: ignore[reportArgumentType]
        assert refused.value.sqlstate == "BRD01"
        assert "is closed and cannot be changed" in str(refused.value)
    assert row_of(owner, item_id) == before


def test_the_owner_may_change_a_closed_item(
    owner: psycopg.Connection, held: dict[str, str]
) -> None:
    owner.execute(CLOSE, (held["nobody"],))
    changed = owner.execute(
        "UPDATE items SET result = %s WHERE id = %s", (Jsonb({"fixed": "by hand"}), held["nobody"])
    ).rowcount
    assert changed == 1


def test_a_superuser_is_not_the_owner_either(
    conn: psycopg.Connection, held: dict[str, str]
) -> None:
    conn.execute(CLOSE, (held["nobody"],))
    with pytest.raises(psycopg.Error) as refused:
        conn.execute("UPDATE items SET urgent = true WHERE id = %s", (held["nobody"],))
    assert refused.value.sqlstate == "BRD01"


def test_only_the_operator_writes_a_reply_and_every_tier_reads_it(
    app_url: str, held: dict[str, str]
) -> None:
    add = "INSERT INTO replies (item_id, author, body) VALUES (%s, %s, %s)"
    item_id = held["session:0a1b2c3d"]
    with as_tier(app_url, OPERATOR) as conn:
        conn.execute(add, (item_id, OPERATOR.identity, "use the spare press"))
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, OPERATOR) as conn:
        conn.execute(add, (item_id, "human:somebody-else", "use the spare press"))
    for caller in (SESSION, RECONCILER):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(add, (item_id, caller.identity, "use the spare press"))
    for caller in TIERS:
        with as_tier(app_url, caller) as conn:
            assert conn.execute("SELECT author, body FROM replies").fetchall() == [
                (OPERATOR.identity, "use the spare press")
            ]
        for statement in ("UPDATE replies SET body = 'x'", "DELETE FROM replies"):
            with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
                conn.execute(statement)


@pytest.fixture
def pr_item(owner: psycopg.Connection, held: dict[str, str]) -> str:
    """An open pr-approve item on example/repo#202 that the operator holds."""
    insert_row(
        owner,
        id="aaaaaaaa",
        kind="pr-approve",
        subject_type="pr",
        subject_ref="202",
        fields=Jsonb({"head_sha": SHA_A, "why": "touches the widget press"}),
    )
    return "aaaaaaaa"


MOVE = "SELECT set_pr_head_sha(%s, %s, %s, %s)"


def changes(
    owner: psycopg.Connection, before: tuple[Any, ...], after: tuple[Any, ...]
) -> dict[str, tuple[Any, Any]]:
    columns = [d.name for d in owner.execute("SELECT * FROM items LIMIT 0").description or []]
    return {c: (b, a) for c, b, a in zip(columns, before, after, strict=True) if b != a}


@pytest.mark.parametrize("caller", [SESSION, RUN, RECONCILER], ids=lambda c: c.identity)
def test_the_head_sha_function_moves_the_sha_and_records_it(
    app_url: str, owner: psycopg.Connection, pr_item: str, caller: Caller
) -> None:
    before = row_of(owner, pr_item)
    with as_tier(app_url, caller) as conn:
        # The direct ways are still closed to this tier: no row, or no privilege.
        assert (
            conn.execute("UPDATE items SET result = '{}' WHERE id = %s", (pr_item,)).rowcount == 0
        )
        assert conn.execute(MOVE, (pr_item, "example/repo", "202", SHA_B)).fetchone() == (True,)
        # And so is the close.
        assert conn.execute(CLOSE, (pr_item,)).rowcount == 0
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        conn.execute("UPDATE items SET fields = '{}' WHERE id = %s", (pr_item,))
    changed = changes(owner, before, row_of(owner, pr_item))
    assert set(changed) == {"fields", "head_sha_moves"}
    assert changed["fields"] == (
        {"head_sha": SHA_A, "why": "touches the widget press"},
        {"head_sha": SHA_B, "why": "touches the widget press"},
    )
    was, moves = changed["head_sha_moves"]
    assert was == []
    assert len(moves) == 1
    assert {k: moves[0][k] for k in ("from", "to", "by")} == {
        "from": SHA_A,
        "to": SHA_B,
        "by": caller.identity,
    }
    assert moves[0]["at"].startswith("20")


def test_every_move_of_the_head_sha_is_kept(
    app_url: str, owner: psycopg.Connection, pr_item: str
) -> None:
    sha_c = "c" * 40
    with as_tier(app_url, SESSION) as conn:
        conn.execute(MOVE, (pr_item, "example/repo", "202", SHA_B))
        # The same SHA again is no move.
        assert conn.execute(MOVE, (pr_item, "example/repo", "202", SHA_B)).fetchone() == (True,)
    with as_tier(app_url, RECONCILER) as conn:
        conn.execute(MOVE, (pr_item, "example/repo", "202", sha_c))
    row = owner.execute("SELECT head_sha_moves FROM items WHERE id = %s", (pr_item,)).fetchone()
    assert row is not None
    assert [(m["from"], m["to"], m["by"]) for m in row[0]] == [
        (SHA_A, SHA_B, "session:0a1b2c3d"),
        (SHA_B, sha_c, "process:reconciler"),
    ]


@pytest.mark.parametrize(
    ("repo", "number"),
    [("example/repo", "203"), ("example/other", "202"), ("example/repo", ""), (None, None)],
)
def test_the_head_sha_function_needs_the_item_s_own_pr(
    app_url: str, owner: psycopg.Connection, pr_item: str, repo: str | None, number: str | None
) -> None:
    """The caller names the PR; an ID that is another PR's item changes nothing and says so."""
    before = row_of(owner, pr_item)
    with as_tier(app_url, SESSION) as conn:
        assert conn.execute(MOVE, (pr_item, repo, number, SHA_B)).fetchone() == (False,)
    assert row_of(owner, pr_item) == before


def test_the_head_sha_function_touches_open_pr_items_only(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], pr_item: str
) -> None:
    # A manual item on the same PR is not a PR item, though its subject matches.
    insert_row(owner, id="mmmmmmmm", subject_type="pr", subject_ref="202")
    owner.execute(
        "UPDATE items SET closed_at = now(), resolution = 'superseded' WHERE id = %s", (pr_item,)
    )
    unchanged = {i: row_of(owner, i) for i in [*held.values(), pr_item, "mmmmmmmm"]}
    with as_tier(app_url, SESSION) as conn:
        for item_id in [*unchanged, "zzzzzzzz"]:
            for number in ("202", "101", "201"):
                moved = conn.execute(MOVE, (item_id, "example/repo", number, SHA_B)).fetchone()
                assert moved == (False,)
    assert {item_id: row_of(owner, item_id) for item_id in unchanged} == unchanged


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "g" * 40, "a" * 41, "a" * 40 + "\n", None])
def test_the_head_sha_function_refuses_what_is_not_a_sha(
    app_url: str, owner: psycopg.Connection, pr_item: str, sha: str | None
) -> None:
    before = row_of(owner, pr_item)
    with pytest.raises(errors.CheckViolation), as_tier(app_url, SESSION) as conn:
        conn.execute(MOVE, (pr_item, "example/repo", "202", sha))
    assert row_of(owner, pr_item) == before


@pytest.mark.parametrize("actor", [None, "", "somebody", "session:", "robot:r2"])
def test_the_head_sha_function_needs_an_actor(
    app_url: str, owner: psycopg.Connection, pr_item: str, actor: str | None
) -> None:
    """A move is always signed: without an identity in board.actor there is none."""
    before = row_of(owner, pr_item)
    with (
        pytest.raises(errors.InsufficientPrivilege, match=r"board\.actor is not set"),
        psycopg.connect(app_url) as conn,
        conn.transaction(),
    ):
        conn.execute("SET LOCAL ROLE board_agent")
        if actor is not None:
            conn.execute("SELECT set_config('board.actor', %s, true)", (actor,))
        conn.execute(MOVE, (pr_item, "example/repo", "202", SHA_B))
    assert row_of(owner, pr_item) == before


def test_the_head_sha_function_is_for_agents_and_processes_only(
    app_url: str, owner: psycopg.Connection, pr_item: str
) -> None:
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, OPERATOR) as conn:
        conn.execute(MOVE, (pr_item, "example/repo", "202", SHA_B))
    row = owner.execute(
        "SELECT prosecdef, proconfig, pg_get_userbyid(proowner), proacl::text "
        "FROM pg_proc WHERE proname = 'set_pr_head_sha'"
    ).fetchone()
    assert row is not None
    security_definer, config, function_owner, acl = row
    assert security_definer is True
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert function_owner == "board_owner"
    assert "board_agent=X" in acl
    assert "board_process=X" in acl
    assert "board_operator" not in acl
    assert "{=X" not in acl and ",=X" not in acl  # nothing for PUBLIC
    definers = owner.execute(
        "SELECT proname FROM pg_proc WHERE prosecdef AND pronamespace = 'public'::regnamespace "
        "ORDER BY proname"
    ).fetchall()
    # Six are triggers' functions, which nobody can call (the cascade, below, the two of
    # the claims and the three of the board's own); the claims' own are pinned in
    # test_claims_sql.py and the board's in test_board_triggers_sql.py.
    assert definers == [
        ("claim_item",),
        ("claims_end_on_close",),
        ("expire_claims",),
        ("items_cascade_close",),
        ("items_note_operator_act",),
        ("items_stop_after_expiries",),
        ("items_wake_assess",),
        ("merge_assess_trigger",),
        ("record_heartbeat",),
        ("release_claim",),
        ("replies_note_operator_act",),
        ("run_of_token",),
        ("set_pr_head_sha",),
    ]


# The second trigger on an open assess item

MERGE = "SELECT merge_assess_trigger(%s, %s::timestamptz, %s::text[])"
SOONER = datetime(2030, 1, 1, tzinfo=UTC)
LATER = datetime(2030, 6, 1, tzinfo=UTC)


@pytest.fixture
def assess_item(owner: psycopg.Connection) -> str:
    """An open `assess` item nobody holds, with a date and one reason."""
    insert_stage_row(
        owner, "aaaaaaaa", assignee="assess", subject_ref="301", not_before=LATER, reasons=["new"]
    )
    return "aaaaaaaa"


def test_the_reconciler_merges_a_trigger_into_an_open_assess_item(
    app_url: str, owner: psycopg.Connection, assess_item: str
) -> None:
    before = row_of(owner, assess_item)
    with as_tier(app_url, RECONCILER) as conn:
        # The direct way is closed to its tier: the columns are not granted.
        with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE items SET reasons = '{changed}' WHERE id = %s", (assess_item,))
        merged = conn.execute(MERGE, (assess_item, SOONER, ["changed", "new", "changed"]))
        assert merged.fetchone() == (True,)
    assert changes(owner, before, row_of(owner, assess_item)) == {
        "not_before": (LATER, SOONER),
        "reasons": (["new"], ["new", "changed"]),
    }
    # A later date does not move it, and no date is the earliest.
    with as_tier(app_url, RECONCILER) as conn:
        assert conn.execute(MERGE, (assess_item, LATER, [])).fetchone() == (True,)
    assert changes(owner, before, row_of(owner, assess_item))["not_before"] == (LATER, SOONER)
    with as_tier(app_url, RECONCILER) as conn:
        assert conn.execute(MERGE, (assess_item, None, ["dated"])).fetchone() == (True,)
    assert changes(owner, before, row_of(owner, assess_item)) == {
        "not_before": (LATER, None),
        "reasons": (["new"], ["new", "changed", "dated"]),
    }


@pytest.mark.parametrize("caller", [NIGHTLY, SESSION, RUN, OPERATOR], ids=lambda c: c.identity)
def test_the_trigger_merge_is_the_reconciler_s_alone(
    app_url: str, owner: psycopg.Connection, assess_item: str, caller: Caller
) -> None:
    """The nightly shares the reconciler's tier and may call the function, which refuses it
    by name; the other tiers have no grant on it."""
    before = row_of(owner, assess_item)
    with pytest.raises(errors.InsufficientPrivilege) as refused, as_tier(app_url, caller) as conn:
        conn.execute(MERGE, (assess_item, SOONER, ["changed"]))
    by_name = "only the reconciler merges a trigger" in str(refused.value)
    assert by_name is (caller is NIGHTLY)
    assert row_of(owner, assess_item) == before


@pytest.mark.parametrize("actor", [None, "", "process:reconcile", "process:reconciler "])
def test_the_trigger_merge_needs_the_reconciler_as_the_actor(
    app_url: str, owner: psycopg.Connection, assess_item: str, actor: str | None
) -> None:
    before = row_of(owner, assess_item)
    with (
        pytest.raises(errors.InsufficientPrivilege, match="only the reconciler"),
        psycopg.connect(app_url) as conn,
        conn.transaction(),
    ):
        conn.execute("SET LOCAL ROLE board_process")
        if actor is not None:
            conn.execute("SELECT set_config('board.actor', %s, true)", (actor,))
        conn.execute(MERGE, (assess_item, SOONER, ["changed"]))
    assert row_of(owner, assess_item) == before


@pytest.mark.parametrize(
    "reasons",
    [
        None,
        [None],
        ["changed", None],
        [""],
        ["Changed"],
        ["has space"],
        ["-changed"],
        ["changed\n"],
        ["x" * 41],
        ["changed"] * 21,
    ],
    ids=repr,
)
def test_the_trigger_merge_refuses_a_bad_or_null_reason(
    app_url: str, owner: psycopg.Connection, assess_item: str, reasons: list[str | None] | None
) -> None:
    before = row_of(owner, assess_item)
    with pytest.raises(errors.CheckViolation), as_tier(app_url, RECONCILER) as conn:
        conn.execute(MERGE, (assess_item, SOONER, reasons))
    assert row_of(owner, assess_item) == before


def test_the_trigger_merge_touches_an_open_unheld_assess_item_only(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], assess_item: str
) -> None:
    """A closed item, an item that is not an `assess` item, an item a run holds and an ID
    that is no item: each returns false and changes nothing."""
    insert_stage_row(owner, "cccccccc", assignee="assess", subject_ref="302", reasons=["new"])
    owner.execute(CLOSE_AS, ("superseded", "cccccccc"))
    insert_stage_row(
        owner, "dddddddd", assignee="assess", subject_ref="303", claimed_by=RUN.identity
    )
    unchanged = {i: row_of(owner, i) for i in [*held.values(), "cccccccc", "dddddddd"]}
    with as_tier(app_url, RECONCILER) as conn:
        for item_id in [*unchanged, "zzzzzzzz"]:
            assert conn.execute(MERGE, (item_id, SOONER, ["changed"])).fetchone() == (False,)
        # The open, unheld one beside them still takes it.
        assert conn.execute(MERGE, (assess_item, SOONER, ["changed"])).fetchone() == (True,)
    assert {item_id: row_of(owner, item_id) for item_id in unchanged} == unchanged


def test_the_trigger_merge_is_the_owner_s_and_for_processes_only(
    owner: psycopg.Connection,
) -> None:
    row = owner.execute(
        "SELECT prosecdef, proconfig, pg_get_userbyid(proowner), proacl::text "
        "FROM pg_proc WHERE proname = 'merge_assess_trigger'"
    ).fetchone()
    assert row is not None
    security_definer, config, function_owner, acl = row
    assert security_definer is True
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert function_owner == "board_owner"
    assert acl == "{board_owner=X/board_owner,board_process=X/board_owner}"


# Waiting, the cascade and the edges

EDGE = "INSERT INTO item_after (item_id, after_id) VALUES (%s, %s)"


def own(caller: Caller) -> str:
    """The key in `held` of the item this caller may update."""
    return "human:operator" if caller is OPERATOR else caller.identity


@pytest.mark.parametrize("caller", [*TIERS, RUN, NIGHTLY], ids=lambda c: c.identity)
@pytest.mark.parametrize("resolution", ["done", "answered"])
def test_no_tier_closes_a_waiting_item_as_a_success(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], caller: Caller, resolution: str
) -> None:
    """The caller's own item, which the policies let it close: the trigger refuses."""
    item_id = held[own(caller)]
    owner.execute(EDGE, (item_id, held["nobody"]))
    before = row_of(owner, item_id)
    with pytest.raises(psycopg.Error) as refused, as_tier(app_url, caller) as conn:
        conn.execute(CLOSE_AS, (resolution, item_id))
    assert refused.value.sqlstate == "BRD02"
    assert f"item {item_id} waits on {held['nobody']} and cannot be closed" in str(refused.value)
    assert row_of(owner, item_id) == before
    # Once what it waits on is closed as a success, the same statement goes through.
    owner.execute(CLOSE_AS, ("done", held["nobody"]))
    with as_tier(app_url, caller) as conn:
        assert conn.execute(CLOSE_AS, (resolution, item_id)).rowcount == 1


@pytest.mark.parametrize("caller", [*TIERS, RUN], ids=lambda c: c.identity)
@pytest.mark.parametrize("resolution", ["rejected", "replied", "superseded"])
def test_every_tier_closes_its_waiting_item_as_anything_else(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], caller: Caller, resolution: str
) -> None:
    item_id = held[own(caller)]
    owner.execute(EDGE, (item_id, held["nobody"]))
    with as_tier(app_url, caller) as conn:
        assert conn.execute(CLOSE_AS, (resolution, item_id)).rowcount == 1


@pytest.mark.parametrize("caller", [SESSION, RUN, RECONCILER], ids=lambda c: c.identity)
@pytest.mark.parametrize("resolution", ["rejected", "replied"])
def test_a_failure_closes_what_waits_on_it_whoever_holds_that(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], caller: Caller, resolution: str
) -> None:
    """The caller may close neither of the two items down the chain; the cascade does."""
    item_id, second, third = held[own(caller)], held["human:operator"], held["nobody"]
    owner.execute(EDGE, (second, item_id))
    owner.execute(EDGE, (third, second))
    bystander = row_of(owner, held["process:nightly"])
    with as_tier(app_url, caller) as conn:
        for other in (second, third):
            if caller is RECONCILER and other == third:
                # The reconciler may close an unheld stage item itself; the cascade still does.
                continue
            assert conn.execute(CLOSE_AS, ("superseded", other)).rowcount == 0
        assert conn.execute(CLOSE_AS, (resolution, item_id)).rowcount == 1
    rows = owner.execute(
        "SELECT id, resolution, result, closed_at = (SELECT closed_at FROM items WHERE id = %s) "
        "FROM items WHERE id = ANY(%s) ORDER BY id",
        (item_id, [second, third]),
    ).fetchall()
    cause = {"cause": "after", "item": item_id, "resolution": resolution}
    assert rows == sorted([(second, "superseded", cause, True), (third, "superseded", cause, True)])
    assert row_of(owner, held["process:nightly"]) == bystander


def test_the_cascade_is_the_owner_s_and_nobody_calls_it(
    app_url: str, owner: psycopg.Connection
) -> None:
    row = owner.execute(
        "SELECT prosecdef, proconfig, pg_get_userbyid(proowner), proacl::text "
        "FROM pg_proc WHERE proname = 'items_cascade_close'"
    ).fetchone()
    assert row is not None
    security_definer, config, function_owner, acl = row
    assert security_definer is True
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert function_owner == "board_owner"
    assert acl == "{board_owner=X/board_owner}"
    for caller in TIERS:
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute("SELECT items_cascade_close()")


@pytest.mark.parametrize("caller", [*TIERS, RUN], ids=lambda c: c.identity)
def test_a_tier_adds_an_edge_only_with_an_item_it_is_writing(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], caller: Caller
) -> None:
    mine = {"created_by": caller.identity, "created_run": run_of(caller)}
    with as_tier(app_url, caller) as conn:
        insert_row(conn, id="aaaaaaaa", **mine)
        conn.execute(EDGE, ("aaaaaaaa", held["nobody"]))
        conn.execute(EDGE, ("aaaaaaaa", held["human:operator"]))
    edges = sorted([("aaaaaaaa", held["human:operator"]), ("aaaaaaaa", held["nobody"])])
    assert owner.execute("SELECT * FROM item_after ORDER BY 1, 2").fetchall() == edges

    # Not once the item is written: its own item of a moment ago is as closed to it ...
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        conn.execute(EDGE, ("aaaaaaaa", held["process:nightly"]))
    # ... as any older item, the one it holds included ...
    for item_id in (held[own(caller)], held["nobody"], held["process:reconciler"]):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(EDGE, (item_id, held["session:9z8y7x6w"]))
    # ... and writing a new item opens no way to an edge on another.
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        insert_row(conn, id="bbbbbbbb", **mine)
        conn.execute(EDGE, (held["nobody"], "bbbbbbbb"))
    assert owner.execute("SELECT * FROM item_after ORDER BY 1, 2").fetchall() == edges
    assert owner.execute("SELECT count(*) FROM items WHERE id = 'bbbbbbbb'").fetchone() == (0,)


def test_an_edge_on_another_caller_s_new_item_is_refused(
    app_url: str, owner: psycopg.Connection, held: dict[str, str]
) -> None:
    """Should board.actor change inside one transaction, the item's writer still decides."""
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, SESSION) as conn:
        insert_row(conn, id="aaaaaaaa", created_by=SESSION.identity, created_run="0a1b2c3d")
        conn.execute("SELECT set_config('board.actor', %s, true)", (OTHER_SESSION.identity,))
        conn.execute(EDGE, ("aaaaaaaa", held["nobody"]))
    assert owner.execute("SELECT count(*) FROM item_after").fetchone() == (0,)


@pytest.mark.parametrize("caller", TIERS, ids=lambda c: c.tier.name)
def test_every_tier_reads_the_edges_and_none_changes_one(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], caller: Caller
) -> None:
    edge = (held[own(caller)], held["nobody"])
    owner.execute(EDGE, edge)
    with as_tier(app_url, caller) as conn:
        assert conn.execute("SELECT * FROM item_after").fetchall() == [edge]
    for statement in (
        "UPDATE item_after SET after_id = 'pppppppp'",
        "UPDATE item_after SET item_id = 'pppppppp'",
        "DELETE FROM item_after",
        "TRUNCATE item_after",
    ):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(statement)
    assert owner.execute("SELECT * FROM item_after").fetchall() == [edge]


def test_the_grants_on_the_edges_are_these_and_no_more(owner: psycopg.Connection) -> None:
    rows = owner.execute(
        "SELECT grantee, string_agg(privilege_type, ',' ORDER BY privilege_type) "
        "FROM information_schema.table_privileges "
        "WHERE table_name = 'item_after' AND grantee <> 'board_owner' "
        "GROUP BY grantee ORDER BY grantee"
    ).fetchall()
    assert rows == [
        ("board_agent", "INSERT,SELECT"),
        ("board_operator", "INSERT,SELECT"),
        ("board_process", "INSERT,SELECT"),
    ]


@pytest.mark.parametrize("caller", [*TIERS, RUN], ids=lambda c: c.identity)
def test_a_tier_writes_holds_with_its_item_and_the_check_still_holds(
    app_url: str, owner: psycopg.Connection, caller: Caller
) -> None:
    mine = {"created_by": caller.identity, "created_run": run_of(caller)}
    with as_tier(app_url, caller) as conn:
        insert_row(conn, id="aaaaaaaa", holds=False, **mine)
    assert owner.execute("SELECT holds FROM items").fetchall() == [(False,)]
    with pytest.raises(errors.CheckViolation) as refused, as_tier(app_url, caller) as conn:
        insert_row(conn, id="bbbbbbbb", holds=False, subject_type="pr", subject_ref="202", **mine)
    assert refused.value.diag.constraint_name == "items_holds_false_on_operator_issue"
