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
| `factory_board/store.py` | The store: items, their edges and replies, read and written as the caller's tier |
| `migrations/` | Numbered SQL migrations, shipped in the image ([rules](migrations/README.md)) |
| `dev/roles.sql` | The five roles, for a throwaway database. Not a migration and not in the image |
| `tests/` | Tests against a real Postgres |

## Roles

Who may change an item is decided in Postgres, by role and row-level security, and not only
in the server's code.

| Role | Login | Is |
|---|---|---|
| `board_owner` | Yes | The owner of the database and the tables. The migrations run as it; the server never does |
| `board_app` | Yes | The server's login. `NOINHERIT`: it can read and write no item until it takes a tier. As itself it has one table, the login proxy's (`oauth_proxy_kv`) |
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
| `board_operator` | Every item, edge and reply | Items and replies, as itself | Any open item: close, defer, bump | No |
| `board_agent` | Every item, edge and reply | Items, as itself and in its own run | An open item it holds itself: close | No |
| `board_process` | Every item, edge and reply | Items, as itself, never `operator_typed` | An open item it holds itself: close | No |

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
  changed after the item was written. It is the only `SECURITY DEFINER` function a tier can
  call and the only way to change anything in `fields`.
- An edge (`item_after`, `migrations/0005_after.sql`) is written with its item: a tier may
  insert one only for an item it created in the same transaction, and none may change or
  delete one. See [Waiting](#waiting).
- `holds` is written with the item and never changed. It says whether an operator's item
  holds its issue (the issue waits on the operator because of it), and may be false only on
  an operator's item about an issue (a CHECK). Nothing reads it yet.
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

## Waiting

An item may be created `after` up to 10 existing items. The edges are set then and never
changed, so there are no cycles.

- **Waiting is derived, not stored**, as "deferred" is from `not_before`: an open item is
  waiting while an item it is after is open. The store returns `after` (every edge) and
  `waiting_on` (the ones still open), and lists waiting items last.
- **Success is required.** An item closed as `done`, `answered` or `superseded` lets go of
  what waits on it. Closing one as `rejected` or `replied` closes every open item that waits
  on it, down the whole chain and in the same transaction, as `superseded` with the result
  `{"cause": "after", "item": <the item whose close started it>, "resolution": ...}`.
- **A waiting item can't be closed as `done` or `answered`**, by anyone (a trigger, SQLSTATE
  `BRD02`; the store raises `ItemWaiting`). It can be rejected, replied to or superseded.
- An item may be created after a closed item, unless that one closed as `rejected` or
  `replied`.
- The cascade is the second `SECURITY DEFINER` function, a trigger's: the items down the
  chain may be held by anyone, and closing them is the board's act, not the caller's.
- A new edge and a cascade take one advisory lock, so an item can't slip in behind a
  reject that is closing what it would wait on.

## Callers

The tier belongs to the token (`factory_board/auth.py`), and is checked on every tool call.

| Caller | Token | Identity | Tier |
|---|---|---|---|
| The operator, in claude.ai | A Pocket ID login in the operator group, through FastMCP's `OIDCProxy` | `human:<username>` | `board_operator` |
| An interactive session | The session token, with the session's ID in the `X-Factory-Board-Session` header | `session:<id>` | `board_agent` |
| The nightly | The nightly token | `process:nightly` | `board_process` |
| The reconciler | The reconciler token | `process:reconciler` | `board_process` |
| The dispatcher | The dispatcher token | `process:dispatcher` | `board_process` |

- A static token is accepted only when the route header is exactly `private`. The private
  route must set that header and the public route must set it to `public`; a route that
  passes the client's own value through gives every tier to whoever knows a token.
- Every session holds the same token, so a session's ID is asserted, not proven.
- `FACTORY_BOARD_DATABASE_URL` must be a URL (`postgresql://…`): the login proxy's store
  connects with asyncpg, which takes no conninfo string.

## Tools

`python -m factory_board serve` serves the MCP at `/mcp`, `/healthz`, and the metrics on a
port of their own. A tool outside the caller's row is refused.

| Tier | Tools |
|---|---|
| Operator | `list_items`, `get_item`, `done`, `answer`, `reply`, `defer`, `reject`, `bump` |
| Session | `create_item`, `list_items`, `get_item`, `update_pr_head` |
| Nightly | `create_keyed_item`, `list_own_items`, `close_own_item` |
| Reconciler | `write_stage_item`, `write_operator_item`, `close_for_fact`, `get_item`, `list_pipeline_items`, `list_assessments` |
| Dispatcher | `list_claimable`, `claim`, `release`, `get_item`, `list_pipeline_items`, `list_assessments` |

The operator and a session also have `list_pipeline_items` and `list_assessments`: the
pipeline's queue can be read by every caller but the nightly. The three processes share a
tier and are told apart by identity.

- `write_stage_item` writes an `assess`, `review`, `merge`, `deploy-check` or continuing
  `work` item, held by nobody. One is open per stage and subject. A second `assess` trigger
  merges into the open item unless a run holds it (`merge_assess_trigger`,
  `migrations/0007_reconciler.sql`): the earlier `not_before` wins and new reasons are
  appended. A `deploy-check` with `closed_as` is written closed, as a record.
- `write_operator_item` writes any operator kind. The reconciler holds a `pr-approve` or
  `pr-merge` item it writes; the operator holds the rest.
- `close_for_fact` closes a PR item the reconciler holds (done or superseded) or a stage
  item nobody holds (superseded only), with `{"cause": <name>}` as the result. The policy
  `items_update_reconciler` allows the second for `process:reconciler` alone.

### Claims

A stage's item is held by nobody until the dispatcher claims it for one run
(`migrations/0008_claims.sql`). The rules are functions in Postgres that run as the owner and
check the caller; no tier may write the claim columns or the counters.

- `list_claimable` first counts the claims that ran out, then lists the queue: every open
  item of a stage that nobody holds, whose `not_before` has come and that waits on no open
  item, without `merge` items. The order is the `claimable` view's rank (1 anything urgent,
  then `health`, `digest`, `review`, `deploy-check`, `assess`, `work`, `retro`), then the
  oldest. `last_claims` are the two claims made last, for the dispatcher's alternation rule.
- `claim` takes the item, a run ID (used once, ever) and the stage's time cap in minutes.
  The run holds the item as `run:<id>` until ten minutes after the cap. The answer carries
  the run's token, once; the `claims` table keeps its SHA-256, in a column no tier can read.
  `run_of_token` says whose token it is while the claim runs: the token is dead when the
  item closes, or the claim is released or runs out. Nothing accepts a run's token over HTTP
  yet; that comes with the stages.
- `claim` takes the issue's current staleness hash for an `assess` item (it is stamped as
  `input_hash`) and for a `work` item that carries one: when they differ, the item closes as
  superseded (`{"cause": "stale"}`), the answer's status is `stale`, and nothing is claimed.
- `release` gives a claim back with a reason (`start-failed`, `infra-window`, `usage-limit`,
  `stopped`, `not-live`). The item returns to the queue at once and `releases` goes up; a
  `deploy-check` may name a `not_before`, at most a day ahead. In the database a run may
  release its own claim too.
- A claim that runs out leaves the item open and unclaimed, with `expiries` one up. A run
  whose claim has run out can no longer close its item, counted or not.
- **Two expiries in a row** on one item (releases in between are skipped) write a `decision`
  item for the operator, about the item's subject and urgent if the item is. The item waits
  on it (the one edge the board writes after an item exists), so it is in no queue. The
  decision's first option, "try again", lets the item go for two more runs. Any other close
  of the decision (its second option, the operator's own text, a reject, a reply) closes the
  item as superseded; `answer` names it under `also_closed`.
- `get_item` shows a stage item's `claims`: each run, when it claimed, and how the claim
  ended (`closed`, `released` with its reason, `expired`).

- `done`, `answer` and `defer` work on an item a person holds. `reply` closes such an item
  as replied; on an item someone else holds it attaches the reply and leaves it open.
  `reject` and `bump` work on any open item.
- `create_item` and `create_keyed_item` take `after`, and `create_item` takes `holds`
  ([Waiting](#waiting)). An item shows `after` and `waiting_on` when it has any, and `holds`
  when it is false. `done` and `answer` are refused on a waiting item. `reject`, and a
  `reply` that closes, answer with `also_closed`: the items the close took with it.
- A session's item is held by `human:<operator>`; the nightly holds its keyed items.
- Every create and verb logs `[BOARD] item=… kind=… verb=… actor=…`, with no item text.
  An item closed by a cascade gets a line of its own, with the verb `cascade`.
- The metrics are read from the database every 30 seconds: `factory_board_open_items` and
  `factory_board_oldest_open_item_created_timestamp_seconds` (by assignee, urgent,
  deferred, waiting), `factory_board_closed_items` (by assignee, resolution),
  `factory_board_issue_open_items` (by repo and issue: the operator's open items that hold
  that issue, deferred and waiting ones included; an issue with none has no series) and
  `factory_board_tool_calls_total`.

## Settings

| Variable | Meaning |
|---|---|
| `FACTORY_BOARD_DATABASE_URL` | The board's Postgres as `board_app`: what the server and the store connect with |
| `FACTORY_BOARD_MIGRATE_DATABASE_URL` | The same database as `board_owner`: what `migrate` connects with. The server does not need it |
| `FACTORY_BOARD_MIGRATIONS_DIR` | Where the migrations are. Defaults to `migrations/` beside the package; the image sets `/app/migrations` |
| `FACTORY_BOARD_TEST_DATABASE_URL` | Tests only: a superuser on a Postgres the tests may empty |
| `FACTORY_BOARD_BASE_URL` | The server's public URL, as claude.ai reaches it |
| `FACTORY_BOARD_OIDC_CONFIG_URL` | Pocket ID's discovery document |
| `FACTORY_BOARD_OIDC_CLIENT_ID`, `FACTORY_BOARD_OIDC_CLIENT_SECRET` | The board's client in Pocket ID |
| `FACTORY_BOARD_JWT_SIGNING_KEY` | Signs the tokens the login proxy issues |
| `FACTORY_BOARD_STORAGE_KEY` | A Fernet key: the login proxy's rows are encrypted with it |
| `FACTORY_BOARD_OPERATOR_GROUP` | The Pocket ID group that is the operator tier. Defaults to `factory-operators` |
| `FACTORY_BOARD_ALLOWED_REDIRECTS` | Redirect URIs a client may register, comma-separated. Defaults to claude.ai's |
| `FACTORY_BOARD_ROUTE_HEADER` | The header the routes set to `private` or `public`, overwriting the client's |
| `FACTORY_BOARD_OPERATOR` | The operator's Pocket ID username. A session's item for the operator is held by `human:<operator>` |
| `FACTORY_BOARD_PORT`, `FACTORY_BOARD_METRICS_PORT` | The MCP's port (8000) and the metrics' (9090) |
| `FACTORY_BOARD_SESSION_TOKEN_SHA256` | The SHA-256 of the interactive sessions' token. Unset, that token is off |
| `FACTORY_BOARD_NIGHTLY_TOKEN_SHA256` | The SHA-256 of the nightly's token. Unset, that token is off |
| `FACTORY_BOARD_RECONCILER_TOKEN_SHA256` | The SHA-256 of the reconciler's token. Unset, that token is off |
| `FACTORY_BOARD_DISPATCHER_TOKEN_SHA256` | The SHA-256 of the dispatcher's token. Unset, that token is off. The four hashes must differ |

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
