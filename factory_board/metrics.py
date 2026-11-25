"""Prometheus metrics. Served on a port of their own, so /metrics is never behind the edge."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

from factory_board.store import Stats

# A static token that matched, on a request that did not come through the private route.
STATIC_TOKEN_REFUSED = Counter(
    "factory_board_static_token_refused_total",
    "A static token presented off the private route",
    ["token"],
)

TOOL_CALLS = Counter("factory_board_tool_calls_total", "MCP tool calls", ["tool", "outcome"])

# Read from the database every REFRESH_SECONDS, so they survive a restart and a restore.
OPEN_ITEMS = Gauge(
    "factory_board_open_items", "Open items", ["assignee", "urgent", "deferred", "waiting"]
)
OLDEST_OPEN_ITEM = Gauge(
    "factory_board_oldest_open_item_created_timestamp_seconds",
    "When the oldest open item was created",
    ["assignee", "urgent", "deferred", "waiting"],
)
CLOSED_ITEMS = Gauge("factory_board_closed_items", "Closed items, ever", ["assignee", "resolution"])
# One series per issue the operator has an open item on that holds it, and none for any other
# issue. Read it together with the refresh timestamp: a missing series means "no item" only
# while the gauges are fresh.
ISSUE_OPEN_ITEMS = Gauge(
    "factory_board_issue_open_items",
    "The operator's open items that hold an issue",
    ["repo", "issue"],
)
# The queue the dispatcher reads: what a run may be started on now. Every stage of the order
# has both series, at 0 when its queue is empty, so a rule never sees "no data".
QUEUE_STAGES = ("health", "digest", "review", "deploy-check", "assess", "work", "retro")
CLAIMABLE_ITEMS = Gauge(
    "factory_board_claimable_items",
    "Stage items a run may be started on now: open, unheld, not deferred, not waiting",
    ["stage", "urgent"],
)
# Not the item's creation: a dated item, a released one and one that waited on another
# entered the queue later. No series for an empty queue.
OLDEST_CLAIMABLE_ITEM = Gauge(
    "factory_board_oldest_claimable_item_timestamp_seconds",
    "Since when the item longest in a stage's queue could have been claimed",
    ["stage", "urgent"],
)
# `ending` is closed, released, expired, or running for a claim that has not ended;
# `reason` is a release's reason and empty otherwise.
CLAIMS = Gauge(
    "factory_board_claims",
    "Claims, ever, by the item's stage and how they ended",
    ["stage", "ending", "reason"],
)
FIX_FORWARD_ISSUES = Gauge(
    "factory_board_issues_at_fix_forward_limit",
    "Issues with three work items from assess since the operator last acted on them",
)
# 0 until the reconciler's first heartbeat: a rule on its age leaves 0 out.
RECONCILER_PASS = Gauge(
    "factory_board_reconciler_pass_timestamp_seconds",
    "When the reconciler last recorded a finished pass",
)
LAST_REFRESH = Gauge(
    "factory_board_metrics_refresh_timestamp_seconds",
    "When the item gauges were last read from the database",
)

REFRESH_SECONDS = 30


def show(stats: Stats) -> None:
    """Set the item gauges from one reading of the database."""
    OPEN_ITEMS.clear()
    OLDEST_OPEN_ITEM.clear()
    CLOSED_ITEMS.clear()
    ISSUE_OPEN_ITEMS.clear()
    # The operator's plain series is always there, so a rule on it never sees "no data".
    OPEN_ITEMS.labels("operator", "false", "false", "false").set(0)
    for row in stats.open:
        labels = (
            row.assignee,
            str(row.urgent).lower(),
            str(row.deferred).lower(),
            str(row.waiting).lower(),
        )
        OPEN_ITEMS.labels(*labels).set(row.count)
        OLDEST_OPEN_ITEM.labels(*labels).set(row.oldest_created)
    for row in stats.closed:
        CLOSED_ITEMS.labels(row.assignee, row.resolution).set(row.count)
    for row in stats.issues:
        ISSUE_OPEN_ITEMS.labels(row.repo, row.issue).set(row.count)
    CLAIMABLE_ITEMS.clear()
    OLDEST_CLAIMABLE_ITEM.clear()
    CLAIMS.clear()
    for stage in QUEUE_STAGES:
        for urgent in ("false", "true"):
            CLAIMABLE_ITEMS.labels(stage, urgent).set(0)
    for row in stats.queue:
        labels = (row.stage, str(row.urgent).lower())
        CLAIMABLE_ITEMS.labels(*labels).set(row.count)
        OLDEST_CLAIMABLE_ITEM.labels(*labels).set(row.oldest_since)
    for row in stats.claims:
        CLAIMS.labels(row.stage, row.ending, row.reason).set(row.count)
    FIX_FORWARD_ISSUES.set(stats.at_fix_forward_limit)
    RECONCILER_PASS.set(stats.reconciler_pass or 0)
    LAST_REFRESH.set_to_current_time()
