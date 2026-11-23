"""The roles and the policies, with plain SQL past the store.

The store is not trusted here: these tests show what Postgres itself lets each tier do.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from helpers import (
    NIGHTLY,
    OPERATOR,
    RECONCILER,
    RUN,
    SESSION,
    SHA_A,
    SHA_B,
    as_tier,
    insert_row,
    row_of,
)
from psycopg import errors
from psycopg.types.json import Jsonb

from factory_board.store import Caller

TIERS = [OPERATOR, SESSION, RECONCILER]
CLOSE = "UPDATE items SET closed_at = now(), resolution = 'done' WHERE id = %s"


@pytest.fixture
def owner(migrated: None, owner_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(owner_url, autocommit=True) as connection:
        yield connection


@pytest.fixture
def held(owner: psycopg.Connection) -> dict[str, str]:
    """One open item per kind of holder; the key is who holds it."""
    ids = {
        "human:operator": "hhhhhhhh",
        "process:reconciler": "pppppppp",
        "process:nightly": "nnnnnnnn",
        "session:0a1b2c3d": "ssssssss",
        "run:20260101-work-01": "rrrrrrrr",
    }
    for number, (holder, item_id) in enumerate(ids.items(), start=101):
        insert_row(owner, id=item_id, claimed_by=holder, subject_ref=str(number))
    insert_row(owner, id="uuuuuuuu", claimed_by=None, subject_ref="199")
    return {**ids, "nobody": "uuuuuuuu"}


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
            conn.execute("SELECT set_pr_head_sha('hhhhhhhh', %s)", (SHA_A,))


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
        (SESSION, "human:operator", False),
        (SESSION, "process:reconciler", False),
        (SESSION, "session:0a1b2c3d", True),
        (SESSION, "run:20260101-work-01", True),
        (SESSION, "nobody", True),
        (RUN, "human:operator", False),
        (RUN, "process:nightly", False),
        (RECONCILER, "process:reconciler", True),
        (RECONCILER, "process:nightly", False),
        (RECONCILER, "human:operator", False),
        (RECONCILER, "session:0a1b2c3d", False),
        (RECONCILER, "nobody", False),
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
    bump = "UPDATE items SET urgent = true WHERE id = %s"
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
    assert owner.execute("SELECT urgent FROM items WHERE id = %s", (item_id,)).fetchone() == (True,)


def test_a_process_with_no_actor_set_updates_nothing(app_url: str, held: dict[str, str]) -> None:
    with psycopg.connect(app_url) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE board_process")
        assert conn.execute("UPDATE items SET urgent = true").rowcount == 0


def test_an_agent_cannot_hand_an_item_to_a_holder_it_could_not_touch(
    app_url: str, held: dict[str, str]
) -> None:
    for holder in ("human:operator", "process:reconciler"):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, SESSION) as conn:
            conn.execute(
                "UPDATE items SET claimed_by = %s WHERE id = %s",
                (holder, held["session:0a1b2c3d"]),
            )


def test_an_upsert_onto_a_refused_row_raises(app_url: str, held: dict[str, str]) -> None:
    """Unlike a plain UPDATE, ON CONFLICT DO UPDATE is not silent, so the store never uses it."""
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, SESSION) as conn:
        conn.execute(
            "INSERT INTO items (id, assignee, subject_repo, subject_type, subject_ref, kind, "
            "created_by) VALUES (%s, 'operator', 'example/repo', 'issue', '101', 'manual', %s) "
            "ON CONFLICT (id) DO UPDATE SET urgent = true",
            (held["human:operator"], SESSION.identity),
        )


@pytest.mark.parametrize("caller", TIERS, ids=lambda c: c.tier.name)
def test_every_tier_inserts_under_its_own_identity_only(
    app_url: str, migrated: None, caller: Caller
) -> None:
    with as_tier(app_url, caller) as conn:
        # An item for the operator, held by the operator: any tier may write one.
        insert_row(conn, created_by=caller.identity, claimed_by="human:operator")
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        insert_row(conn, id="bbbbbbbb", created_by="human:somebody-else")


@pytest.mark.parametrize("caller", TIERS, ids=lambda c: c.tier.name)
def test_no_tier_deletes(app_url: str, held: dict[str, str], caller: Caller) -> None:
    for statement in ("DELETE FROM items", "TRUNCATE items", "DELETE FROM replies"):
        with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
            conn.execute(statement)


@pytest.mark.parametrize("caller", TIERS, ids=lambda c: c.tier.name)
@pytest.mark.parametrize(
    "assignment",
    [
        "id = 'zzzzzzzz'",
        "assignee = 'work'",
        "kind = 'decision'",
        "subject_ref = '999'",
        "key = 'other'",
        "created_by = 'human:operator'",
        "created_at = now()",
        "operator_typed = true",
    ],
)
def test_no_tier_rewrites_what_an_item_is(
    app_url: str, held: dict[str, str], caller: Caller, assignment: str
) -> None:
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, caller) as conn:
        conn.execute(f"UPDATE items SET {assignment} WHERE id = %s", (held["nobody"],))  # pyright: ignore[reportArgumentType]


def test_the_owner_may_delete(owner: psycopg.Connection, held: dict[str, str]) -> None:
    assert owner.execute("DELETE FROM items WHERE id = %s", (held["nobody"],)).rowcount == 1


@pytest.mark.parametrize("caller", TIERS, ids=lambda c: c.tier.name)
def test_a_closed_item_cannot_be_changed(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], caller: Caller
) -> None:
    # Unheld, or held by the caller: the policies would let every one of these through.
    item_id = held["process:reconciler"] if caller is RECONCILER else held["nobody"]
    owner.execute(CLOSE, (item_id,))
    before = row_of(owner, item_id)
    for statement in (
        "UPDATE items SET closed_at = NULL, resolution = NULL WHERE id = %s",
        "UPDATE items SET resolution = 'rejected' WHERE id = %s",
        "UPDATE items SET urgent = true WHERE id = %s",
        "UPDATE items SET result = '{}' WHERE id = %s",
    ):
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
    """An open pr-approve item that the operator holds."""
    insert_row(
        owner,
        id="aaaaaaaa",
        kind="pr-approve",
        subject_type="pr",
        subject_ref="202",
        fields=Jsonb({"head_sha": SHA_A, "why": "touches the widget press"}),
    )
    return "aaaaaaaa"


@pytest.mark.parametrize("caller", [SESSION, RUN, RECONCILER], ids=lambda c: c.identity)
def test_the_head_sha_function_changes_the_sha_and_nothing_else(
    app_url: str, owner: psycopg.Connection, pr_item: str, caller: Caller
) -> None:
    before = row_of(owner, pr_item)
    with as_tier(app_url, caller) as conn:
        # The direct way is still closed to this tier.
        assert (
            conn.execute("UPDATE items SET urgent = true WHERE id = %s", (pr_item,)).rowcount == 0
        )
        assert conn.execute("SELECT set_pr_head_sha(%s, %s)", (pr_item, SHA_B)).fetchone() == (
            True,
        )
        # And so is the close.
        assert conn.execute(CLOSE, (pr_item,)).rowcount == 0
    after = row_of(owner, pr_item)
    columns = [d.name for d in owner.execute("SELECT * FROM items LIMIT 0").description or []]
    changed = {c: (b, a) for c, b, a in zip(columns, before, after, strict=True) if b != a}
    assert changed == {
        "fields": (
            {"head_sha": SHA_A, "why": "touches the widget press"},
            {"head_sha": SHA_B, "why": "touches the widget press"},
        )
    }


def test_the_head_sha_function_touches_open_pr_items_only(
    app_url: str, owner: psycopg.Connection, held: dict[str, str], pr_item: str
) -> None:
    owner.execute(
        "UPDATE items SET closed_at = now(), resolution = 'superseded' WHERE id = %s", (pr_item,)
    )
    unchanged = {item_id: row_of(owner, item_id) for item_id in [*held.values(), pr_item]}
    with as_tier(app_url, SESSION) as conn:
        for item_id in [*unchanged, "zzzzzzzz"]:
            assert conn.execute("SELECT set_pr_head_sha(%s, %s)", (item_id, SHA_B)).fetchone() == (
                False,
            )
    assert {item_id: row_of(owner, item_id) for item_id in unchanged} == unchanged


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "g" * 40, "a" * 41, None])
def test_the_head_sha_function_refuses_what_is_not_a_sha(
    app_url: str, pr_item: str, sha: str | None
) -> None:
    with pytest.raises(errors.CheckViolation), as_tier(app_url, SESSION) as conn:
        conn.execute("SELECT set_pr_head_sha(%s, %s)", (pr_item, sha))


def test_the_head_sha_function_is_for_agents_and_processes_only(
    app_url: str, owner: psycopg.Connection, pr_item: str
) -> None:
    with pytest.raises(errors.InsufficientPrivilege), as_tier(app_url, OPERATOR) as conn:
        conn.execute("SELECT set_pr_head_sha(%s, %s)", (pr_item, SHA_B))
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
        "SELECT proname FROM pg_proc WHERE prosecdef AND pronamespace = 'public'::regnamespace"
    ).fetchall()
    assert definers == [("set_pr_head_sha",)]
