# factory-board

The board: an MCP server with Postgres that holds one table of items. An item is one thing
someone has to do. At first that someone is the operator (approve this PR, run this command,
pick an option); later the same table is the job queue of an agent pipeline.

It is built for one homelab's pipeline and is not a general to-do service.

## Status

The scaffold and the store: settings, the migration runner, the item schema with its roles
and policies, and the store's primitives. The kinds' typed fields, auth, the operator's verbs
and the MCP tools come in later PRs; nothing serves requests yet.

## Layout

| Path | Holds |
|---|---|
| `factory_board/config.py` | Settings, read from the environment |
| `factory_board/migrate.py` | The migration runner |
| `factory_board/store.py` | The store: items and replies, read and written as the caller's tier |
| `migrations/` | Numbered SQL migrations, shipped in the image ([rules](migrations/README.md)) |
| `dev/roles.sql` | The five roles, for a throwaway database. Not a migration and not in the image |
| `tests/` | Tests against a real Postgres |

## Roles

Who may change an item is decided in Postgres, by role and row-level security, and not only
in the server's code.

| Role | Login | Is |
|---|---|---|
| `board_owner` | Yes | The owner of the database and the tables. The migrations run as it; the server never does |
| `board_app` | Yes | The server's login. `NOINHERIT`: it can read and write nothing until it takes a tier |
| `board_operator` | No | Tier: the operator |
| `board_agent` | No | Tier: interactive sessions, and later the pipeline's runs |
| `board_process` | No | Tier: the deterministic scripts |

The store starts every transaction with `SET LOCAL ROLE <tier>` and
`set_config('board.actor', '<identity>', true)`. Both end with the transaction, so a pooled
connection carries neither to the next caller. An identity is `human:<name>`,
`process:<name>`, `run:<id>` or `session:<id>`.

What each tier may do with a row (`migrations/0003_access.sql`):

| Tier | SELECT | INSERT | UPDATE | DELETE |
|---|---|---|---|---|
| `board_operator` | Every item and reply | Items and replies, as itself | Any open item: close, defer, bump | No |
| `board_agent` | Every item and reply | Items, as itself and in its own run | An open item it holds itself: close | No |
| `board_process` | Every item and reply | Items, as itself, never `operator_typed` | An open item it holds itself: close | No |

- "As itself": `created_by`, or a reply's `author`, must equal `board.actor`. For an agent,
  `created_run` must be the ID in its identity. Whether the operator typed into the run
  (`operator_typed`) is the agent's word, as its session ID is.
- "Holds itself": `claimed_by` equals `board.actor`. An item nobody holds can be changed by
  the operator only; claiming comes with a later build step.
- A new item is open: no tier can insert a close, a result, a counted claim or a head SHA move.
- An item for the operator is always held by a `human:` or `process:` identity (a CHECK), so
  no agent can change it. A session hands a step over by creating an item the operator holds.
- The grants are by column. Every tier may write `closed_at`, `resolution` and `result`; the
  operator also `not_before` and `urgent`. Nothing else: not `fields`, not the claim, not what
  the item is. A later step that writes another column brings its own grant.
- A closed item is final: a trigger refuses every later change, except by the owner.
- One exception to the holder rule: `set_pr_head_sha(item, repo, pr, head_sha)` lets an agent
  or a process move the head SHA of an open `pr-approve` or `pr-merge` item that someone else
  holds. The caller names the PR, and it must be the item's. Each move is appended to the
  item's `head_sha_moves` (old SHA, new SHA, who, when), so the operator sees that the SHA
  changed after the item was written. It is the only `SECURITY DEFINER` function and the only
  way to change anything in `fields`.
- Postgres refuses an `UPDATE` under these policies silently: no row changes and nothing is
  raised. The store checks the row count and raises `NotClaimHolder`, `ItemClosed` or
  `ItemNotFound`.
- The database trusts `board.actor`, which the server sets. The server must take the caller,
  and with it `created_by`, `created_run` and `operator_typed`, from the authenticated token
  and never from a tool's input.

**The migrations create no role.** In a cluster the roles are declared in the cluster's own
configuration, where the database's owner can't create any. The first migration stops with
the missing role's name if one is not there. For a throwaway database, a superuser runs
[`dev/roles.sql`](dev/roles.sql) first: it creates the five roles with fixed passwords and
gives the database to `board_owner`.

## Settings

| Variable | Meaning |
|---|---|
| `FACTORY_BOARD_DATABASE_URL` | The board's Postgres as `board_app`: what the server and the store connect with |
| `FACTORY_BOARD_MIGRATE_DATABASE_URL` | The same database as `board_owner`: what `migrate` connects with. The server does not need it |
| `FACTORY_BOARD_MIGRATIONS_DIR` | Where the migrations are. Defaults to `migrations/` beside the package; the image sets `/app/migrations` |
| `FACTORY_BOARD_TEST_DATABASE_URL` | Tests only: a superuser on a Postgres the tests may empty |

`python -m factory_board migrate` applies the pending migrations and exits. A command asks
only for the URL it needs, and fails by name when that one is missing.

## Development

The recipes run in the image's `dev` stage with podman, so they use the Python the image
ships, and the tests get a throwaway Postgres 17.

```
just lint           # ruff check and ruff format --check
just typecheck      # pyright, strict
just test           # pytest against a throwaway Postgres
just build          # the production image
just migrate-check  # the image's migrations on a fresh database, as board_owner, twice
just check          # all of the above
```

The tests need a real Postgres and fail without one; nothing is skipped or faked. To run them
outside `just`, point `FACTORY_BOARD_TEST_DATABASE_URL` at a superuser on an empty database and
run `pytest`.

The fixtures use that superuser for two things only: to run `dev/roles.sql` once (it creates
the roles and makes `board_owner` the database's owner), and to drop and recreate the `public`
schema before every test. The code under test never connects with it: the migrations run as
`board_owner` and the store as `board_app`, with the URLs the fixtures derive from the one
variable and the passwords in `dev/roles.sql`.

Tests and fixtures use made-up issue numbers, repos, commands and names. Nothing in this repo
comes from a real tracker or a real cluster; keep it that way.

## CI

`.github/workflows/ci.yml` runs ruff, pyright and the tests against a Postgres service, then
builds the image, creates the roles on a fresh database with `dev/roles.sql` and applies the
image's migrations to it as `board_owner`, twice. A push to `main` also pushes
the image to `ghcr.io/sometimeskind/factory-board` (`latest` and `sha-<short>`).
