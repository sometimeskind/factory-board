# factory-board

The board: an MCP server with Postgres that holds one table of items. An item is one thing
someone has to do. At first that someone is the operator (approve this PR, run this command,
pick an option); later the same table is the job queue of an agent pipeline.

It is built for one homelab's pipeline and is not a general to-do service.

## Status

The scaffold: settings, the migration runner, tests and CI. The item schema, the kinds, auth
and the MCP tools come in later PRs.

## Layout

| Path | Holds |
|---|---|
| `factory_board/config.py` | Settings, read from the environment |
| `factory_board/migrate.py` | The migration runner |
| `migrations/` | Numbered SQL migrations, shipped in the image ([rules](migrations/README.md)) |
| `tests/` | Tests against a real Postgres |

## Settings

| Variable | Meaning |
|---|---|
| `FACTORY_BOARD_DATABASE_URL` | The board's Postgres. Required |
| `FACTORY_BOARD_MIGRATIONS_DIR` | Where the migrations are. Defaults to `migrations/` beside the package; the image sets `/app/migrations` |
| `FACTORY_BOARD_TEST_DATABASE_URL` | Tests only: a Postgres the tests may empty |

`python -m factory_board migrate` applies the pending migrations and exits.

## Development

The recipes run in the image's `dev` stage with podman, so they use the Python the image
ships, and the tests get a throwaway Postgres 17.

```
just lint        # ruff check and ruff format --check
just typecheck   # pyright, strict
just test        # pytest against a throwaway Postgres
just build       # the production image
just check       # all of the above
```

The tests need a real Postgres and fail without one; nothing is skipped or faked. To run them
outside `just`, point `FACTORY_BOARD_TEST_DATABASE_URL` at an empty database (every test drops
and recreates its `public` schema) and run `pytest`.

Tests and fixtures use made-up issue numbers, repos, commands and names. Nothing in this repo
comes from a real tracker or a real cluster; keep it that way.

## CI

`.github/workflows/ci.yml` runs ruff, pyright and the tests against a Postgres service, then
builds the image and applies its migrations to a fresh database. A push to `main` also pushes
the image to `ghcr.io/sometimeskind/factory-board` (`latest` and `sha-<short>`).
