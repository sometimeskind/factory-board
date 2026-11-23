"""Entry point: `python -m factory_board migrate`, and `python -m factory_board serve`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from factory_board.config import ConfigError, ServerSettings, Settings
from factory_board.migrate import MigrationError, apply_migrations

log = logging.getLogger(__name__)


def migrate() -> int:
    try:
        settings = Settings.from_env()
        applied = apply_migrations(settings.migrate_url(), settings.migrations_dir)
    except (ConfigError, MigrationError) as exc:
        print(f"factory-board: {exc}", file=sys.stderr)
        return 1
    for label in applied:
        print(f"applied {label}")
    if not applied:
        print("nothing to apply")
    return 0


async def serve(database_url: str, settings: ServerSettings) -> None:
    """The MCP on the port, the metrics on a port of their own."""
    import asyncpg  # pyright: ignore[reportMissingTypeStubs]
    from prometheus_client import start_http_server

    from factory_board import metrics
    from factory_board.auth import build_auth, identity_resolver, postgres_store
    from factory_board.server import build_server
    from factory_board.store import Caller, Store, Tier, make_pool

    # Reads counts only; it holds no item and writes nothing.
    reader = Caller(Tier.PROCESS, "process:metrics")

    async def refresh(store: Store) -> None:
        while True:
            try:
                metrics.show(await store.stats(reader))
            except Exception:
                log.exception("could not read the item counts")
            await asyncio.sleep(metrics.REFRESH_SECONDS)

    untyped: object = asyncpg
    proxy_pool = await untyped.create_pool(database_url, min_size=1, max_size=2)  # type: ignore[attr-defined]
    try:
        async with make_pool(database_url) as pool:
            store = Store(pool)
            auth, static = build_auth(settings, postgres_store(proxy_pool))
            mcp = build_server(
                store,
                identity_resolver(settings.operator_group, static),
                settings.operator,
                auth=auth,
            )
            # A plain socket on "::" also accepts IPv4.
            start_http_server(settings.metrics_port, addr="::")
            refreshing = asyncio.create_task(refresh(store))
            try:
                await mcp.run_async(transport="http", host=settings.host, port=settings.port)
            finally:
                refreshing.cancel()
    finally:
        await proxy_pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory-board")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply the pending migrations and exit")
    commands.add_parser("serve", help="serve the MCP and the metrics")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        return migrate()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        database_url = Settings.from_env().serve_url()
        settings = ServerSettings.from_env()
    except ConfigError as exc:
        print(f"factory-board: {exc}", file=sys.stderr)
        return 1
    asyncio.run(serve(database_url, settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
