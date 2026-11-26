"""Made-up callers and items for the tests, and raw SQL as one tier.

Every repo, number, name and command here is invented.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql

from factory_board.store import Caller, NewItem, Subject, Tier

OPERATOR = Caller(Tier.OPERATOR, "human:operator")
SESSION = Caller(Tier.AGENT, "session:0a1b2c3d")
OTHER_SESSION = Caller(Tier.AGENT, "session:9z8y7x6w")
RUN = Caller(Tier.AGENT, "run:20260101-work-01")
RECONCILER = Caller(Tier.PROCESS, "process:reconciler")
NIGHTLY = Caller(Tier.PROCESS, "process:nightly")
DISPATCHER = Caller(Tier.PROCESS, "process:dispatcher")

REPO = "example/repo"
SHA_A = "a" * 40
SHA_B = "b" * 40


def issue(number: int, repo: str = REPO) -> Subject:
    return Subject("issue", str(number), repo)


def pr(number: int, repo: str = REPO) -> Subject:
    return Subject("pr", str(number), repo)


def manual(number: int = 101, *, held_by: str = OPERATOR.identity) -> NewItem:
    """A step for the operator on a made-up issue. A person or a process holds it."""
    return NewItem(
        assignee="operator",
        kind="manual",
        subject=issue(number),
        fields={"command": "make widgets", "why": "the widgets are out"},
        machine="workstation",
        claimed_by=held_by,
    )


def pr_approve(number: int = 202, head_sha: str = SHA_A, *, kind: str = "pr-approve") -> NewItem:
    return NewItem(
        assignee="operator",
        kind=kind,
        subject=pr(number),
        fields={"head_sha": head_sha, "why": "touches the widget press"},
        machine="browser",
        claimed_by=OPERATOR.identity,
    )


def stage(stage_name: str, number: int = 101, *, held_by: str | None = None) -> NewItem:
    return NewItem(assignee=stage_name, kind=stage_name, subject=issue(number), claimed_by=held_by)


def keyed(key: str = "finding:widget-press-hot") -> NewItem:
    return NewItem(
        assignee="operator",
        kind="decision",
        key=key,
        fields={"options": ["cool it", "leave it"]},
        claimed_by=OPERATOR.identity,
    )


@contextmanager
def as_tier(app_url: str, caller: Caller) -> Generator[psycopg.Connection]:
    """A transaction on a connection of its own, as the caller's tier: raw SQL past the store."""
    with psycopg.connect(app_url) as conn, conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(caller.tier.value)))
        conn.execute("SELECT set_config('board.actor', %s, true)", (caller.identity,))
        yield conn


# A valid row, column by column. A test overrides the one value it is about.
VALID_ROW: dict[str, Any] = {
    "id": "abcdefgh",
    "assignee": "operator",
    "subject_repo": REPO,
    "subject_type": "issue",
    "subject_ref": "101",
    "kind": "manual",
    "claimed_by": "human:operator",
    "created_by": "session:0a1b2c3d",
}


def insert_row(conn: psycopg.Connection, **overrides: Any) -> None:
    """Insert one row with plain SQL; `overrides` replace or add columns."""
    row = {**VALID_ROW, **overrides}
    query = sql.SQL("INSERT INTO items ({}) VALUES ({})").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in row),
        sql.SQL(", ").join(sql.Placeholder() for _ in row),
    )
    conn.execute(query, list(row.values()))


def insert_stage_row(conn: psycopg.Connection, item_id: str, **overrides: Any) -> None:
    """A stage's item (a `work` item unless told otherwise), which anyone or nobody may hold."""
    stage_name = overrides.pop("assignee", "work")
    insert_row(
        conn,
        **{"id": item_id, "assignee": stage_name, "kind": stage_name, "claimed_by": None}
        | overrides,
    )


def row_of(conn: psycopg.Connection, item_id: str) -> tuple[Any, ...]:
    """The whole row, to compare before and after."""
    row = conn.execute("SELECT * FROM items WHERE id = %s", (item_id,)).fetchone()
    assert row is not None
    return row


# The seats a made-up run sits in: nine capitals for each stage's runs, in order.
RUN_SEATS = {
    "health": ["lisbon", "madrid", "paris", "rome", "vienna", "berlin", "oslo", "dublin", "prague"],
    "digest": [
        "athens",
        "warsaw",
        "budapest",
        "sofia",
        "riga",
        "tallinn",
        "vilnius",
        "bern",
        "kyiv",
    ],
    "review": ["tokyo", "seoul", "hanoi", "manila", "jakarta", "bangkok", "dhaka", "kabul", "doha"],
    "deploy-check": [
        "ottawa",
        "lima",
        "quito",
        "bogota",
        "caracas",
        "havana",
        "santiago",
        "asuncion",
        "panama",
    ],
    "assess": [
        "cairo",
        "nairobi",
        "accra",
        "dakar",
        "rabat",
        "tunis",
        "algiers",
        "luanda",
        "abuja",
    ],
    "work": [
        "canberra",
        "wellington",
        "suva",
        "apia",
        "honiara",
        "majuro",
        "tarawa",
        "funafuti",
        "buenos-aires",
    ],
    "retro": [
        "ankara",
        "tehran",
        "baghdad",
        "amman",
        "beirut",
        "riyadh",
        "muscat",
        "baku",
        "minsk",
    ],
}

# The seats free_seat hands out: none is a run's, so the two never meet.
FREE_SEATS = [
    "brussels",
    "amsterdam",
    "copenhagen",
    "stockholm",
    "helsinki",
    "reykjavik",
    "valletta",
    "nicosia",
    "luxembourg",
    "ljubljana",
    "zagreb",
    "belgrade",
    "bucharest",
    "chisinau",
]


def seat_of(run: str) -> str:
    """The seat a made-up run sits in, a normal one: `20260101-review-02` sits in `seoul`."""
    stage_name, _, number = run.split("-", 1)[1].rpartition("-")
    return RUN_SEATS[stage_name][int(number) - 1]


def free_seat(conn: psycopg.Connection) -> str:
    """The first of FREE_SEATS that no running claim sits in, a normal one."""
    taken = {row[0] for row in conn.execute("SELECT seat FROM claims WHERE ended_at IS NULL")}
    return next(seat for seat in FREE_SEATS if seat not in taken)
