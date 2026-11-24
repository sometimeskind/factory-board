"""The store: the items and their replies, read and written as the caller's tier.

Every method takes the caller and runs in one transaction that starts with
`SET LOCAL ROLE <tier>` and `set_config('board.actor', <identity>, true)`. The pool's
login (board_app) has no rights on these tables, so nothing here works outside such a
transaction, and both settings end with it: the next caller on the same pooled connection
starts with neither.

Who may change which row is decided by the policies in migrations/0003_access.sql, not
here. Postgres refuses such an UPDATE silently (no row changes, no error), so every write
checks that a row came back and raises a typed error if none did.

These are primitives. Which verb closes what, which fields a kind carries and who may
call what are the layers above.

The store believes its caller. The server must build the `Caller` from the authenticated
token, and take `created_by`, `created_run` and `operator_typed` from the token too, never
from a tool's input: the policies bind what is written to board.actor, and board.actor is
whatever the server sets.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg import errors, sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

# No 0, 1, i, l or o: an ID is read aloud and typed on a phone.
ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
ID_LENGTH = 8

PR_KINDS = ("pr-approve", "pr-merge")

# The SQLSTATE the items_closed_is_final trigger raises.
_CLOSED_SQLSTATE = "BRD01"
# And the items_waiting_is_not_done trigger's.
_WAITING_SQLSTATE = "BRD02"

# How many items one item may wait on.
MAX_AFTER = 10

# An item as the store reads it: its row, the items it is after, and the ones of those
# that are still open. Waiting is derived here and stored nowhere; a closed item waits on
# nothing.
_ITEM = sql.SQL(
    """
    items.*,
    ARRAY(
        SELECT a.after_id FROM item_after AS a WHERE a.item_id = items.id
    ) AS after,
    ARRAY(
        SELECT a.after_id
        FROM item_after AS a JOIN items AS before ON before.id = a.after_id
        WHERE a.item_id = items.id AND items.closed_at IS NULL AND before.closed_at IS NULL
    ) AS waiting_on
    """
)

_HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# A fresh ID collides about once in 10^11 items, and a conflicting item can close between
# the insert and the look for it. Either way one more try settles it.
_CREATE_ATTEMPTS = 5

type Connection = psycopg.AsyncConnection[tuple[Any, ...]]
type Pool = AsyncConnectionPool[Connection]


class Tier(StrEnum):
    """A tier is a Postgres role; the value is the role's name."""

    OPERATOR = "board_operator"
    AGENT = "board_agent"
    PROCESS = "board_process"


_TIER_PREFIXES: dict[Tier, tuple[str, ...]] = {
    Tier.OPERATOR: ("human:",),
    Tier.AGENT: ("session:", "run:"),
    Tier.PROCESS: ("process:",),
}


class Resolution(StrEnum):
    DONE = "done"
    ANSWERED = "answered"
    REPLIED = "replied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class StoreError(Exception):
    """The store could not do what was asked."""


class ItemNotFound(StoreError):
    """There is no item with this ID."""


class ItemClosed(StoreError):
    """The item is closed. A closed item is never changed again."""


class NotPermitted(StoreError):
    """The caller's tier may not do this."""


class NotClaimHolder(NotPermitted):
    """The item is held by someone else, and the caller's tier may not change it."""


class InvalidItem(StoreError):
    """The database refused a value: a bad ID, assignee, kind, identity or the like."""


class ItemWaiting(StoreError):
    """The item waits on an open item, and so can't be closed as done or answered."""


@dataclass(frozen=True)
class Caller:
    """Who is asking: the tier their token carries, and their identity."""

    tier: Tier
    identity: str

    def __post_init__(self) -> None:
        prefixes = _TIER_PREFIXES[self.tier]
        if not self.identity.startswith(prefixes):
            raise ValueError(
                f"a {self.tier.name.lower()} caller's identity starts with "
                f"{' or '.join(prefixes)}, not {self.identity!r}"
            )


@dataclass(frozen=True)
class Subject:
    """What an item is about. An alert or a finding has no repo."""

    type: str
    ref: str
    repo: str | None = None


@dataclass(frozen=True)
class NewItem:
    """What a creator supplies. The ID, the creator and the times are the store's."""

    assignee: str
    kind: str
    subject: Subject | None = None
    key: str | None = None
    fields: dict[str, Any] = field(default_factory=dict[str, Any])
    machine: str | None = None
    claimed_by: str | None = None
    not_before: datetime | None = None
    urgent: bool = False
    reasons: tuple[str, ...] = ()
    input_hash: str | None = None
    created_run: str | None = None
    operator_typed: bool = False
    # The items this one waits on. Set here and never changed.
    after: tuple[str, ...] = ()
    # Whether an operator's item holds its issue. Only an item on an issue can say no.
    holds: bool = True


@dataclass(frozen=True)
class Item:
    id: str
    assignee: str
    subject: Subject | None
    kind: str
    fields: dict[str, Any]
    machine: str | None
    claimed_by: str | None
    claim_expires: datetime | None
    not_before: datetime | None
    urgent: bool
    key: str | None
    result: dict[str, Any] | None
    closed_at: datetime | None
    resolution: Resolution | None
    expiries: int
    releases: int
    reasons: tuple[str, ...]
    input_hash: str | None
    head_sha_moves: tuple[dict[str, Any], ...]
    created_by: str
    created_run: str | None
    operator_typed: bool
    created_at: datetime
    holds: bool
    # Every item this one is after, and the ones of them that are still open. Both sorted.
    after: tuple[str, ...]
    waiting_on: tuple[str, ...]

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True)
class Created:
    """The outcome of a create: the new item, or the open item that was already there."""

    item: Item
    existing: bool


@dataclass(frozen=True)
class Reply:
    id: UUID
    item_id: str
    author: str
    created_at: datetime
    body: str


@dataclass(frozen=True)
class OpenCount:
    assignee: str
    urgent: bool
    deferred: bool
    waiting: bool
    count: int
    # When the oldest of them was created, in seconds since the epoch.
    oldest_created: float


@dataclass(frozen=True)
class ClosedCount:
    assignee: str
    resolution: str
    count: int


@dataclass(frozen=True)
class IssueCount:
    """The operator's open items on one issue that hold it, deferred and waiting ones included."""

    repo: str
    issue: str
    count: int


@dataclass(frozen=True)
class Stats:
    open: tuple[OpenCount, ...]
    closed: tuple[ClosedCount, ...]
    issues: tuple[IssueCount, ...]


def new_item_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


def make_pool(database_url: str, *, min_size: int = 1, max_size: int = 4) -> Pool:
    """A pool for the store, not yet open: `await pool.open()`, or use it as a context."""
    return AsyncConnectionPool(database_url, min_size=min_size, max_size=max_size, open=False)


def _item(row: DictRow) -> Item:
    subject = None
    if row["subject_type"] is not None:
        subject = Subject(row["subject_type"], row["subject_ref"], row["subject_repo"])
    resolution = row["resolution"]
    return Item(
        id=row["id"],
        assignee=row["assignee"],
        subject=subject,
        kind=row["kind"],
        fields=row["fields"],
        machine=row["machine"],
        claimed_by=row["claimed_by"],
        claim_expires=row["claim_expires"],
        not_before=row["not_before"],
        urgent=row["urgent"],
        key=row["key"],
        result=row["result"],
        closed_at=row["closed_at"],
        resolution=None if resolution is None else Resolution(resolution),
        expiries=row["expiries"],
        releases=row["releases"],
        reasons=tuple(row["reasons"]),
        input_hash=row["input_hash"],
        head_sha_moves=tuple(row["head_sha_moves"]),
        created_by=row["created_by"],
        created_run=row["created_run"],
        operator_typed=row["operator_typed"],
        created_at=row["created_at"],
        holds=row["holds"],
        after=tuple(sorted(row["after"])),
        waiting_on=tuple(sorted(row["waiting_on"])),
    )


def _reply(row: DictRow) -> Reply:
    return Reply(row["id"], row["item_id"], row["author"], row["created_at"], row["body"])


def _translate(exc: psycopg.Error) -> StoreError | None:
    """The store's error for a database error, or None for one it does not know."""
    if exc.sqlstate == _CLOSED_SQLSTATE:
        return ItemClosed(exc.diag.message_primary or "the item is closed")
    if exc.sqlstate == _WAITING_SQLSTATE:
        return ItemWaiting(exc.diag.message_primary or "the item is waiting")
    if isinstance(exc, errors.InsufficientPrivilege):
        return NotPermitted(exc.diag.message_primary or "not permitted")
    if isinstance(exc, errors.CheckViolation):
        return InvalidItem(
            f"refused by {exc.diag.constraint_name}"
            if exc.diag.constraint_name
            else exc.diag.message_primary or "refused by a check"
        )
    return None


class Store:
    def __init__(self, pool: Pool, *, new_id: Callable[[], str] = new_item_id) -> None:
        self._pool = pool
        self._new_id = new_id

    @asynccontextmanager
    async def _as(self, caller: Caller) -> AsyncGenerator[Connection]:
        """One transaction as the caller's tier, with board.actor set to their identity."""
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(
                    sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(caller.tier.value))
                )
                await conn.execute("SELECT set_config('board.actor', %s, true)", (caller.identity,))
                yield conn
        except psycopg.Error as exc:
            translated = _translate(exc)
            if translated is None:
                raise
            raise translated from exc

    @staticmethod
    def _run_of(caller: Caller) -> str | None:
        """An agent's run or session ID, which is the part of its identity after the colon."""
        if caller.tier is Tier.AGENT:
            return caller.identity.partition(":")[2]
        return None

    @staticmethod
    async def _fetch(conn: Connection, item_id: str) -> Item | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            sql.SQL("SELECT {} FROM items WHERE id = %s").format(_ITEM), (item_id,)
        )
        row = await cur.fetchone()
        return None if row is None else _item(row)

    async def _refusal(self, conn: Connection, item_id: str) -> StoreError:
        """Why an UPDATE of an open item changed no row."""
        item = await self._fetch(conn, item_id)
        if item is None:
            return ItemNotFound(f"no item {item_id}")
        if not item.is_open:
            return ItemClosed(f"item {item_id} is closed")
        return NotClaimHolder(
            f"item {item_id} is held by {item.claimed_by or 'nobody'}; this tier may not change it"
        )

    async def _update_open(
        self, conn: Connection, item_id: str, assignments: sql.Composable, params: dict[str, Any]
    ) -> Item:
        """Change one open item, or raise the reason no row changed."""
        query = sql.SQL(
            "UPDATE items SET {} WHERE id = %(id)s AND closed_at IS NULL RETURNING {}"
        ).format(assignments, _ITEM)
        cur = await conn.cursor(row_factory=dict_row).execute(query, {**params, "id": item_id})
        row = await cur.fetchone()
        if row is None:
            raise await self._refusal(conn, item_id)
        return _item(row)

    async def create_item(self, caller: Caller, new: NewItem) -> Created:
        """Insert an item, or return the open item that already stands in its place.

        The existing item is returned as it is, with one exception: a new head SHA on a
        PR item is written to the existing item, since that is what a second push means
        (not for the operator tier, which does not push). What the new item was to wait on
        is not added to the existing one: an item's edges are written with it and never
        after.
        """
        if len(new.after) > MAX_AFTER:
            raise InvalidItem(f"an item waits on at most {MAX_AFTER} items, not {len(new.after)}")
        if len(set(new.after)) != len(new.after):
            raise InvalidItem("an item it is to wait on is named twice")
        subject = new.subject
        params: dict[str, Any] = {
            "assignee": new.assignee,
            "subject_repo": None if subject is None else subject.repo,
            "subject_type": None if subject is None else subject.type,
            "subject_ref": None if subject is None else subject.ref,
            "kind": new.kind,
            "fields": Jsonb(new.fields),
            "machine": new.machine,
            "claimed_by": new.claimed_by,
            "not_before": new.not_before,
            "urgent": new.urgent,
            "key": new.key,
            "reasons": list(new.reasons),
            "input_hash": new.input_hash,
            "created_by": caller.identity,
            "created_run": new.created_run or self._run_of(caller),
            "operator_typed": new.operator_typed,
            "holds": new.holds,
        }
        for _ in range(_CREATE_ATTEMPTS):
            async with self._as(caller) as conn:
                cur = await conn.cursor(row_factory=dict_row).execute(
                    """
                    INSERT INTO items (
                        id, assignee, subject_repo, subject_type, subject_ref, kind, fields,
                        machine, claimed_by, not_before, urgent, key, reasons, input_hash,
                        created_by, created_run, operator_typed, holds
                    ) VALUES (
                        %(id)s, %(assignee)s, %(subject_repo)s, %(subject_type)s,
                        %(subject_ref)s, %(kind)s, %(fields)s, %(machine)s, %(claimed_by)s,
                        %(not_before)s, %(urgent)s, %(key)s, %(reasons)s, %(input_hash)s,
                        %(created_by)s, %(created_run)s, %(operator_typed)s, %(holds)s
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    {**params, "id": self._new_id()},
                )
                row = await cur.fetchone()
                if row is not None:
                    await self._add_after(conn, row["id"], new.after)
                    item = await self._fetch(conn, row["id"])
                    assert item is not None
                    return Created(item, existing=False)
                existing = await self._find_open_twin(conn, new)
                if existing is None:
                    continue
                head_sha = new.fields.get("head_sha")
                if (
                    caller.tier is not Tier.OPERATOR
                    and existing.kind in PR_KINDS
                    and new.kind in PR_KINDS
                    and isinstance(head_sha, str)
                    and existing.fields.get("head_sha") != head_sha
                ):
                    assert new.subject is not None
                    existing = await self._set_head_sha(conn, existing.id, new.subject, head_sha)
                return Created(existing, existing=True)
        raise StoreError(f"no free item ID after {_CREATE_ATTEMPTS} tries")

    @staticmethod
    async def _add_after(conn: Connection, item_id: str, after: tuple[str, ...]) -> None:
        """Write the edges of the item this transaction has just inserted.

        The policy allows them for no other item. Raising here takes the item back too. An
        item closed as rejected or replied is refused by the database, as an InvalidItem.
        """
        if not after:
            return
        cur = await conn.execute("SELECT id FROM items WHERE id = ANY(%s)", (list(after),))
        known = {row[0] for row in await cur.fetchall()}
        missing = [after_id for after_id in after if after_id not in known]
        if missing:
            raise ItemNotFound(f"no item {', '.join(missing)} to wait on")
        await conn.execute(
            "INSERT INTO item_after (item_id, after_id) SELECT %s, unnest(%s::text[])",
            (item_id, list(after)),
        )

    @staticmethod
    async def _find_open_twin(conn: Connection, new: NewItem) -> Item | None:
        """The open item that one of the three uniqueness rules puts in the new one's way."""
        subject = new.subject
        cur = await conn.cursor(row_factory=dict_row).execute(
            sql.SQL(
                """
            SELECT {} FROM items
            WHERE closed_at IS NULL AND (
                key = %(key)s::text
                OR (
                    kind IN ('pr-approve', 'pr-merge')
                    AND %(kind)s::text IN ('pr-approve', 'pr-merge')
                    AND subject_repo = %(repo)s::text
                    AND subject_ref = %(ref)s::text
                )
                OR (
                    assignee <> 'operator'
                    AND assignee = %(assignee)s::text
                    AND subject_type = %(type)s::text
                    AND subject_ref = %(ref)s::text
                    AND subject_repo IS NOT DISTINCT FROM %(repo)s::text
                )
            )
            ORDER BY created_at
            LIMIT 1
            """
            ).format(_ITEM),
            {
                "key": new.key,
                "kind": new.kind,
                "assignee": new.assignee,
                "type": None if subject is None else subject.type,
                "ref": None if subject is None else subject.ref,
                "repo": None if subject is None else subject.repo,
            },
        )
        row = await cur.fetchone()
        return None if row is None else _item(row)

    async def get_item(self, caller: Caller, item_id: str) -> Item:
        async with self._as(caller) as conn:
            item = await self._fetch(conn, item_id)
        if item is None:
            raise ItemNotFound(f"no item {item_id}")
        return item

    async def list_open(
        self, caller: Caller, *, assignee: str | None = None, include_deferred: bool = False
    ) -> list[Item]:
        """The open items: what can be done now before what waits, then urgent ones first,
        then the oldest.

        An item whose `not_before` is still ahead is deferred: it is open, but left out
        unless `include_deferred` is set. A waiting item is not left out: the operator may
        still reject it, and sees what it waits on.
        """
        async with self._as(caller) as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                sql.SQL(
                    """
                SELECT * FROM (
                    SELECT {} FROM items
                    WHERE closed_at IS NULL
                      AND (%(assignee)s::text IS NULL OR assignee = %(assignee)s::text)
                      AND (%(include_deferred)s OR not_before IS NULL OR not_before <= now())
                ) AS listed
                ORDER BY cardinality(waiting_on) > 0, urgent DESC, created_at, id
                """
                ).format(_ITEM),
                {"assignee": assignee, "include_deferred": include_deferred},
            )
            return [_item(row) for row in await cur.fetchall()]

    async def close_item(
        self,
        caller: Caller,
        item_id: str,
        resolution: Resolution,
        result: dict[str, Any] | None = None,
    ) -> Item:
        """Close an open item. Raises NotClaimHolder when the policy refuses the caller.

        A waiting item is not closed as done or answered (ItemWaiting). Closing as rejected
        or replied also closes, in the database, every open item down the chain that waits
        on this one: `list_cascaded` names them.
        """
        async with self._as(caller) as conn:
            return await self._update_open(
                conn,
                item_id,
                sql.SQL("closed_at = now(), resolution = %(resolution)s, result = %(result)s"),
                {
                    "resolution": resolution.value,
                    "result": None if result is None else Jsonb(result),
                },
            )

    async def list_cascaded(self, caller: Caller, item_id: str) -> list[Item]:
        """The items that closed because this one was rejected or replied to, oldest first.

        A closer writes its own result, so the result alone proves nothing: the cascade's
        items also closed in the transaction that closed this one, at the same moment.
        """
        async with self._as(caller) as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                sql.SQL(
                    """
                SELECT {} FROM items
                WHERE resolution = 'superseded'
                  AND result @> %(cause)s
                  AND closed_at = (SELECT closed_at FROM items AS first WHERE first.id = %(id)s)
                ORDER BY created_at, id
                """
                ).format(_ITEM),
                {"cause": Jsonb({"cause": "after", "item": item_id}), "id": item_id},
            )
            return [_item(row) for row in await cur.fetchall()]

    async def set_not_before(self, caller: Caller, item_id: str, when: datetime | None) -> Item:
        """Defer an open item, or with None end the deferral. The operator tier only."""
        async with self._as(caller) as conn:
            return await self._update_open(
                conn, item_id, sql.SQL("not_before = %(when)s"), {"when": when}
            )

    async def set_urgent(self, caller: Caller, item_id: str, urgent: bool) -> Item:
        """Bump an open item, or take the bump back. The operator tier only."""
        async with self._as(caller) as conn:
            return await self._update_open(
                conn, item_id, sql.SQL("urgent = %(urgent)s"), {"urgent": urgent}
            )

    async def set_head_sha(self, caller: Caller, item_id: str, pr: Subject, head_sha: str) -> Item:
        """Move the head SHA of an open pr-approve or pr-merge item, whoever holds it.

        For the agent and process tiers: whoever pushes again. The caller names the PR as
        well as the item, and the two must agree. The move is recorded on the item
        (`head_sha_moves`). No tier may update `fields` itself, and the database function
        behind this is not granted to the operator.
        """
        async with self._as(caller) as conn:
            return await self._set_head_sha(conn, item_id, pr, head_sha)

    async def _set_head_sha(
        self, conn: Connection, item_id: str, pr: Subject, head_sha: str
    ) -> Item:
        if not _HEAD_SHA_RE.match(head_sha):
            raise InvalidItem("a head SHA is 40 lower-case hex characters")
        cur = await conn.execute(
            "SELECT set_pr_head_sha(%s, %s, %s, %s)", (item_id, pr.repo, pr.ref, head_sha)
        )
        if await cur.fetchone() == (True,):
            item = await self._fetch(conn, item_id)
            if item is not None:
                return item
        refusal = await self._refusal(conn, item_id)
        if isinstance(refusal, NotClaimHolder):
            # Open and found, so it is not a PR item, or not that PR's.
            raise InvalidItem(
                f"item {item_id} is not a pr-approve or pr-merge item on {pr.repo}#{pr.ref}"
            )
        raise refusal

    async def add_reply(self, caller: Caller, item_id: str, body: str) -> Reply:
        """Add a reply to an item. Only the operator tier may."""
        try:
            async with self._as(caller) as conn:
                cur = await conn.cursor(row_factory=dict_row).execute(
                    "INSERT INTO replies (item_id, author, body) VALUES (%s, %s, %s) RETURNING *",
                    (item_id, caller.identity, body),
                )
                row = await cur.fetchone()
        except errors.ForeignKeyViolation as exc:
            raise ItemNotFound(f"no item {item_id}") from exc
        assert row is not None
        return _reply(row)

    async def reply_and_close(self, caller: Caller, item_id: str, body: str) -> tuple[Reply, Item]:
        """Add a reply and close the item as replied, in one transaction: both or neither.

        What waits on the item closes with it, as after a reject (`list_cascaded`).
        """
        try:
            async with self._as(caller) as conn:
                cur = await conn.cursor(row_factory=dict_row).execute(
                    "INSERT INTO replies (item_id, author, body) VALUES (%s, %s, %s) RETURNING *",
                    (item_id, caller.identity, body),
                )
                row = await cur.fetchone()
                assert row is not None
                item = await self._update_open(
                    conn,
                    item_id,
                    sql.SQL("closed_at = now(), resolution = %(resolution)s"),
                    {"resolution": Resolution.REPLIED.value},
                )
        except errors.ForeignKeyViolation as exc:
            raise ItemNotFound(f"no item {item_id}") from exc
        return _reply(row), item

    async def stats(self, caller: Caller) -> Stats:
        """Counts for the metrics: open items, closed ones and per issue, and no item's text."""
        async with self._as(caller) as conn:
            cur = await conn.execute(
                """
                SELECT assignee, urgent, deferred, waiting,
                       count(*), min(extract(epoch FROM created_at))::float8
                FROM (
                    SELECT assignee, urgent, created_at,
                           (not_before > now()) IS TRUE AS deferred,
                           EXISTS (
                               SELECT FROM item_after AS a
                               JOIN items AS before ON before.id = a.after_id
                               WHERE a.item_id = items.id AND before.closed_at IS NULL
                           ) AS waiting
                    FROM items WHERE closed_at IS NULL
                ) AS open_items
                GROUP BY 1, 2, 3, 4
                """
            )
            open_items = [OpenCount(*row) for row in await cur.fetchall()]
            cur = await conn.execute(
                """
                SELECT assignee, resolution, count(*)
                FROM items WHERE closed_at IS NOT NULL
                GROUP BY 1, 2
                """
            )
            closed_items = [ClosedCount(*row) for row in await cur.fetchall()]
            # An issue waits on the operator while one of these is open. A deferred or a
            # waiting item still counts: the issue is the operator's either way.
            cur = await conn.execute(
                """
                SELECT subject_repo, subject_ref, count(*)
                FROM items
                WHERE closed_at IS NULL AND assignee = 'operator'
                      AND subject_type = 'issue' AND holds
                GROUP BY 1, 2
                """
            )
            issues = [IssueCount(*row) for row in await cur.fetchall()]
        return Stats(tuple(open_items), tuple(closed_items), tuple(issues))

    async def list_replies(self, caller: Caller, item_id: str) -> list[Reply]:
        """An item's replies, oldest first."""
        async with self._as(caller) as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                "SELECT * FROM replies WHERE item_id = %s ORDER BY created_at, id", (item_id,)
            )
            return [_reply(row) for row in await cur.fetchall()]
