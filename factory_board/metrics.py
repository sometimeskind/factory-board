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
OPEN_ITEMS = Gauge("factory_board_open_items", "Open items", ["assignee", "urgent", "deferred"])
OLDEST_OPEN_ITEM = Gauge(
    "factory_board_oldest_open_item_created_timestamp_seconds",
    "When the oldest open item was created",
    ["assignee", "urgent", "deferred"],
)
CLOSED_ITEMS = Gauge("factory_board_closed_items", "Closed items, ever", ["assignee", "resolution"])
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
    # The operator's plain series is always there, so a rule on it never sees "no data".
    OPEN_ITEMS.labels("operator", "false", "false").set(0)
    for row in stats.open:
        labels = (row.assignee, str(row.urgent).lower(), str(row.deferred).lower())
        OPEN_ITEMS.labels(*labels).set(row.count)
        OLDEST_OPEN_ITEM.labels(*labels).set(row.oldest_created)
    for row in stats.closed:
        CLOSED_ITEMS.labels(row.assignee, row.resolution).set(row.count)
    LAST_REFRESH.set_to_current_time()
