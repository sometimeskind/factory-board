"""What the tables refuse by themselves, whoever writes: plain SQL as the tables' owner."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from helpers import insert_row, insert_stage_row
from psycopg import errors
from psycopg.types.json import Jsonb


@pytest.fixture
def owner(migrated: None, owner_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(owner_url, autocommit=True) as connection:
        yield connection


def count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM items").fetchone()
    assert row is not None
    return row[0]


def test_a_valid_row_is_accepted(owner: psycopg.Connection) -> None:
    insert_row(owner)
    assert count(owner) == 1


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        # The ID: eight characters of the alphabet without look-alikes.
        ({"id": "abcdefg"}, "items_id_format"),
        ({"id": "abcdefghj"}, "items_id_format"),
        ({"id": "ABCDEFGH"}, "items_id_format"),
        ({"id": "abcdefg0"}, "items_id_format"),
        ({"id": "abcdefg1"}, "items_id_format"),
        ({"id": "abcdefgl"}, "items_id_format"),
        ({"id": "abcdefgo"}, "items_id_format"),
        ({"id": "abcdefgi"}, "items_id_format"),
        ({"id": "abcd-fgh"}, "items_id_format"),
        ({"id": "12345678"}, "items_id_format"),
        # Assignee and kind.
        ({"assignee": "somebody"}, "items_assignee_allowed"),
        ({"assignee": "Operator"}, "items_assignee_allowed"),
        ({"kind": "wish"}, "items_kind_allowed"),
        ({"kind": "work"}, "items_kind_fits_assignee"),
        ({"assignee": "work", "kind": "review"}, "items_kind_fits_assignee"),
        ({"assignee": "review", "kind": "manual"}, "items_kind_fits_assignee"),
        ({"kind": "pr-approve"}, "items_pr_kind_on_pr"),
        # An identity is a known prefix, a colon and a name.
        ({"claimed_by": "operator"}, "items_claimed_by_identity"),
        ({"claimed_by": "human"}, "items_claimed_by_identity"),
        ({"claimed_by": "human:"}, "items_claimed_by_identity"),
        ({"claimed_by": "Human:operator"}, "items_claimed_by_identity"),
        ({"claimed_by": " human:operator"}, "items_claimed_by_identity"),
        ({"claimed_by": "human :operator"}, "items_claimed_by_identity"),
        ({"claimed_by": "x\nhuman:operator"}, "items_claimed_by_identity"),
        ({"claimed_by": "human:operator\n"}, "items_claimed_by_identity"),
        ({"claimed_by": "robot:r2"}, "items_claimed_by_identity"),
        ({"claimed_by": "process:a b"}, "items_claimed_by_identity"),
        # The operator's item is held by a person or a process.
        ({"claimed_by": None}, "items_operator_item_holder"),
        ({"claimed_by": "session:0a1b2c3d"}, "items_operator_item_holder"),
        ({"claimed_by": "run:20260101-work-01"}, "items_operator_item_holder"),
        ({"head_sha_moves": Jsonb({"not": "a list"})}, "items_head_sha_moves_array"),
        ({"created_by": "somebody"}, "items_created_by_identity"),
        ({"created_by": "PROCESS:reconciler"}, "items_created_by_identity"),
        # The rest of the fixed lists and shapes.
        ({"machine": "toaster"}, "items_machine_allowed"),
        ({"subject_type": "rumour"}, "items_subject_type_allowed"),
        ({"subject_repo": "not a repo"}, "items_subject_repo_format"),
        ({"subject_ref": "one-oh-one"}, "items_subject_number"),
        ({"subject_ref": None}, "items_subject_complete"),
        ({"subject_repo": None}, "items_subject_complete"),
        ({"subject_type": None}, "items_subject_complete"),
        (
            {"subject_type": None, "subject_ref": None, "subject_repo": None},
            "items_subject_or_key",
        ),
        ({"key": ""}, "items_key_not_empty"),
        ({"fields": Jsonb(["a", "list"])}, "items_fields_object"),
        ({"result": Jsonb("a string")}, "items_result_object"),
        ({"expiries": -1}, "items_expiries_not_negative"),
        ({"releases": -1}, "items_releases_not_negative"),
        ({"claimed_by": None, "claim_expires": "2030-01-01"}, "items_claim_expires_needs_holder"),
        # Open or closed and nothing else.
        ({"closed_at": "2030-01-01"}, "items_closed_with_resolution"),
        ({"resolution": "done"}, "items_closed_with_resolution"),
        ({"closed_at": "2030-01-01", "resolution": "undone"}, "items_resolution_allowed"),
        ({"closed_at": "2030-01-01", "resolution": "Done"}, "items_resolution_allowed"),
        # Only an operator's item on an issue can say it does not hold the issue.
        (
            {"holds": False, "assignee": "work", "kind": "work", "claimed_by": None},
            "items_holds_false_on_operator_issue",
        ),
        (
            {"holds": False, "subject_type": "pr", "subject_ref": "202"},
            "items_holds_false_on_operator_issue",
        ),
        (
            {"holds": False, "subject_repo": None, "subject_type": "alert"},
            "items_holds_false_on_operator_issue",
        ),
        (
            {
                "holds": False,
                "subject_repo": None,
                "subject_type": None,
                "subject_ref": None,
                "key": "finding:widget-press-hot",
            },
            "items_holds_false_on_operator_issue",
        ),
    ],
)
def test_a_bad_value_is_refused(
    owner: psycopg.Connection, overrides: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(errors.CheckViolation) as refused:
        insert_row(owner, **overrides)
    assert refused.value.diag.constraint_name == constraint
    assert count(owner) == 0


@pytest.mark.parametrize("resolution", ["done", "answered", "replied", "rejected", "superseded"])
def test_the_five_resolutions_are_accepted(owner: psycopg.Connection, resolution: str) -> None:
    insert_row(owner, closed_at="2030-01-01", resolution=resolution)
    assert count(owner) == 1


@pytest.mark.parametrize(
    "identity",
    ["human:operator", "process:reconciler", "run:20260101-work-01", "session:0a1b2c3d"],
)
def test_the_four_identity_prefixes_are_accepted(owner: psycopg.Connection, identity: str) -> None:
    insert_stage_row(owner, "abcdefgh", claimed_by=identity, created_by=identity)
    assert count(owner) == 1


@pytest.mark.parametrize("holder", ["human:operator", "process:nightly"])
def test_the_operator_s_item_may_be_held_by_a_person_or_a_process(
    owner: psycopg.Connection, holder: str
) -> None:
    insert_row(owner, claimed_by=holder)
    assert count(owner) == 1


def test_an_alert_or_a_keyed_item_needs_no_repo(owner: psycopg.Connection) -> None:
    insert_row(
        owner, id="aaaaaaaa", subject_repo=None, subject_type="alert", subject_ref="WidgetHot"
    )
    insert_row(
        owner,
        id="bbbbbbbb",
        subject_repo=None,
        subject_type=None,
        subject_ref=None,
        key="finding:widget-press-hot",
    )
    assert count(owner) == 2


def test_a_second_open_stage_item_on_one_subject_is_refused(owner: psycopg.Connection) -> None:
    insert_row(owner, id="aaaaaaaa", assignee="work", kind="work", claimed_by=None)
    with pytest.raises(errors.UniqueViolation) as refused:
        insert_row(owner, id="bbbbbbbb", assignee="work", kind="work", claimed_by=None)
    assert refused.value.diag.constraint_name == "items_open_stage_subject"
    # Another stage, another issue and another repo are other subjects.
    insert_row(owner, id="cccccccc", assignee="review", kind="review", claimed_by=None)
    insert_row(
        owner, id="dddddddd", assignee="work", kind="work", claimed_by=None, subject_ref="102"
    )
    insert_row(
        owner,
        id="eeeeeeee",
        assignee="work",
        kind="work",
        claimed_by=None,
        subject_repo="example/other",
    )


def test_a_second_open_stage_item_on_one_alert_is_refused(owner: psycopg.Connection) -> None:
    """The subject has no repo here, and a null repo still counts as the same subject."""
    alert = {"subject_repo": None, "subject_type": "alert", "subject_ref": "WidgetHot"}
    insert_row(owner, id="aaaaaaaa", assignee="assess", kind="assess", claimed_by=None, **alert)
    with pytest.raises(errors.UniqueViolation):
        insert_row(owner, id="bbbbbbbb", assignee="assess", kind="assess", claimed_by=None, **alert)


def test_keyed_stage_items_with_no_subject_do_not_collide(owner: psycopg.Connection) -> None:
    none = {"subject_repo": None, "subject_type": None, "subject_ref": None, "claimed_by": None}
    insert_row(owner, id="aaaaaaaa", assignee="health", kind="health", key="health:day-1", **none)
    insert_row(owner, id="bbbbbbbb", assignee="health", kind="health", key="health:day-2", **none)
    assert count(owner) == 2


def test_the_operator_may_have_several_open_items_on_one_issue(owner: psycopg.Connection) -> None:
    insert_row(owner, id="aaaaaaaa")
    insert_row(owner, id="bbbbbbbb")
    insert_row(owner, id="cccccccc", kind="decision")
    assert count(owner) == 3


def test_one_open_pr_item_per_pr(owner: psycopg.Connection) -> None:
    pr = {"subject_type": "pr", "subject_ref": "202"}
    insert_row(owner, id="aaaaaaaa", kind="pr-approve", **pr)
    for kind in ("pr-approve", "pr-merge"):
        with pytest.raises(errors.UniqueViolation) as refused:
            insert_row(owner, id="bbbbbbbb", kind=kind, **pr)
        assert refused.value.diag.constraint_name == "items_open_pr_item"
    # Another kind of item on the same PR is not a PR item.
    insert_row(owner, id="cccccccc", kind="manual", **pr)
    owner.execute(
        "UPDATE items SET closed_at = now(), resolution = 'superseded' WHERE id = 'aaaaaaaa'"
    )
    insert_row(owner, id="bbbbbbbb", kind="pr-merge", **pr)


def test_one_open_item_per_key(owner: psycopg.Connection) -> None:
    insert_row(owner, id="aaaaaaaa", key="finding:widget-press-hot")
    with pytest.raises(errors.UniqueViolation) as refused:
        insert_row(owner, id="bbbbbbbb", key="finding:widget-press-hot", subject_ref="999")
    assert refused.value.diag.constraint_name == "items_open_key"
    owner.execute("UPDATE items SET closed_at = now(), resolution = 'done' WHERE id = 'aaaaaaaa'")
    insert_row(owner, id="bbbbbbbb", key="finding:widget-press-hot")


def test_a_reply_needs_an_item_an_identity_and_a_text(owner: psycopg.Connection) -> None:
    insert_row(owner)
    add = "INSERT INTO replies (item_id, author, body) VALUES (%s, %s, %s)"
    owner.execute(add, ("abcdefgh", "human:operator", "try the other press"))
    with pytest.raises(errors.ForeignKeyViolation):
        owner.execute(add, ("zzzzzzzz", "human:operator", "try the other press"))
    with pytest.raises(errors.CheckViolation):
        owner.execute(add, ("abcdefgh", "operator", "try the other press"))
    with pytest.raises(errors.CheckViolation):
        owner.execute(add, ("abcdefgh", "human:operator", ""))


def test_an_item_holds_its_issue_unless_it_says_not(owner: psycopg.Connection) -> None:
    insert_row(owner, id="aaaaaaaa")
    insert_row(owner, id="bbbbbbbb", holds=False)
    rows = owner.execute("SELECT id, holds FROM items ORDER BY id").fetchall()
    assert rows == [("aaaaaaaa", True), ("bbbbbbbb", False)]


EDGE = "INSERT INTO item_after (item_id, after_id) VALUES (%s, %s)"


def test_an_edge_joins_two_different_items_once(owner: psycopg.Connection) -> None:
    insert_row(owner, id="aaaaaaaa")
    insert_row(owner, id="bbbbbbbb")
    owner.execute(EDGE, ("bbbbbbbb", "aaaaaaaa"))
    with pytest.raises(errors.UniqueViolation):
        owner.execute(EDGE, ("bbbbbbbb", "aaaaaaaa"))
    with pytest.raises(errors.CheckViolation) as refused:
        owner.execute(EDGE, ("bbbbbbbb", "bbbbbbbb"))
    assert refused.value.diag.constraint_name == "item_after_not_itself"
    for edge in (("bbbbbbbb", "zzzzzzzz"), ("zzzzzzzz", "aaaaaaaa")):
        with pytest.raises(errors.ForeignKeyViolation):
            owner.execute(EDGE, edge)
    assert owner.execute("SELECT * FROM item_after").fetchall() == [("bbbbbbbb", "aaaaaaaa")]


@pytest.mark.parametrize("resolution", ["done", "answered", "replied", "rejected", "superseded"])
def test_no_edge_points_at_a_failure(owner: psycopg.Connection, resolution: str) -> None:
    """Whoever writes it, the owner included: nothing waits on a rejected or replied item."""
    insert_row(owner, id="aaaaaaaa", closed_at="2030-01-01", resolution=resolution)
    insert_row(owner, id="bbbbbbbb")
    if resolution in ("rejected", "replied"):
        with pytest.raises(errors.CheckViolation, match=f"aaaaaaaa was closed as {resolution}"):
            owner.execute(EDGE, ("bbbbbbbb", "aaaaaaaa"))
    else:
        owner.execute(EDGE, ("bbbbbbbb", "aaaaaaaa"))


@pytest.mark.parametrize("resolution", ["done", "answered"])
def test_the_owner_does_not_close_a_waiting_item_as_a_success_either(
    owner: psycopg.Connection, resolution: str
) -> None:
    insert_row(owner, id="aaaaaaaa")
    insert_row(owner, id="bbbbbbbb")
    insert_row(owner, id="cccccccc")
    owner.execute(EDGE, ("cccccccc", "aaaaaaaa"))
    owner.execute(EDGE, ("cccccccc", "bbbbbbbb"))
    close = "UPDATE items SET closed_at = now(), resolution = %s WHERE id = %s"
    with pytest.raises(psycopg.Error) as refused:
        owner.execute(close, (resolution, "cccccccc"))
    assert refused.value.sqlstate == "BRD02"
    assert f"item cccccccc waits on aaaaaaaa, bbbbbbbb and cannot be closed as {resolution}" in str(
        refused.value
    )
    owner.execute(close, ("done", "aaaaaaaa"))
    owner.execute(close, ("superseded", "bbbbbbbb"))
    assert owner.execute(close, (resolution, "cccccccc")).rowcount == 1


def test_the_tables_are_forced_under_row_security(owner: psycopg.Connection) -> None:
    rows = owner.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner) "
        "FROM pg_class WHERE relname IN ('items', 'replies', 'item_after') ORDER BY relname"
    ).fetchall()
    assert rows == [
        ("item_after", True, True, "board_owner"),
        ("items", True, True, "board_owner"),
        ("replies", True, True, "board_owner"),
    ]


def test_item_ids_come_from_no_sequence(owner: psycopg.Connection) -> None:
    assert owner.execute("SELECT count(*) FROM pg_sequences").fetchone() == (0,)
