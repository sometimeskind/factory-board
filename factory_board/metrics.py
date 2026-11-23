"""Prometheus metrics. Served on a port of their own, so /metrics is never behind the edge."""

from __future__ import annotations

from prometheus_client import Counter

# A static token that matched, on a request that did not come through the private route.
STATIC_TOKEN_REFUSED = Counter(
    "factory_board_static_token_refused_total",
    "A static token presented off the private route",
    ["token"],
)
