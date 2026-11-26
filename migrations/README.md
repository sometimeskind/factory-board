# Migrations

Plain SQL, applied in order by `factory_board/migrate.py` and shipped in the image.

- Name a file `NNNN_<name>.sql`: four digits from `0001`, then lower-case letters, digits and `_`.
  No gaps and no duplicate numbers.
- Each file runs in one transaction with its `schema_migrations` row, so don't write
  `BEGIN`/`COMMIT`, and don't use statements that can't run in a transaction
  (`CREATE INDEX CONCURRENTLY`).
- Never edit, rename or delete a file that has been applied anywhere: the runner checks each
  applied file's SHA-256 and refuses to start. Fix a mistake with a new migration.

- A migration creates no role. It grants to the roles and writes policies for them, and
  `0001` stops with a clear message when one is missing ([Roles](../README.md#roles)).
- The migrations run as the tables' owner. `items`, `replies`, `item_after`, `claims`,
  `operator_acts`, `heartbeats` and `pause` are under
  `FORCE ROW LEVEL SECURITY`, so the owner reads and writes them through the `*_owner`
  policies; a new table under row security needs one too, or a later migration's `UPDATE` on
  it changes no row and says nothing.
- The tiers' grants on `items` are by column. A migration that adds a column, or a step that
  starts writing one, grants it to the tiers that need it and to no other.

| File | Holds |
|---|---|
| `0001_items.sql` | The role check; the `items` table with its checks, the three uniqueness rules, the list index, and the trigger that makes a closed item final |
| `0002_replies.sql` | The `replies` table |
| `0003_access.sql` | The column grants, row-level security and its policies, and `set_pr_head_sha` |
| `0004_oauth_proxy.sql` | The login proxy's key-value table, the one table `board_app` uses as itself |
| `0005_after.sql` | The `holds` column; the `item_after` table (which item waits on which) with its grants and policies; the triggers that keep a waiting item from closing as done and that close what waits on a rejected item |
| `0006_machine_other.sql` | The machine `other`, for a step done on none of the listed machines |
| `0007_reconciler.sql` | The reconciler's policy (it closes a stage item nobody holds) and `merge_assess_trigger` |
| `0008_claims.sql` | The `claims` table (one row per claim, with the run token's hash); the `claimable` view, which is the queue's order; `claim_item`, `release_claim` and `expire_claims` for the dispatcher, with the decision item after two expiries in a row; `run_of_token` |
| `0009_board_triggers.sql` | What the board does by itself: the trigger that wakes assess when the operator's last item or the last `deploy-check` on an issue closes, and `pending_reasons` for a wake that meets a held `assess` item; `operator_acts` (when the operator last acted on an issue) and the fix-forward limit that counts from it (`fix_forward`, SQLSTATE `BRD04`); the `heartbeats` table and `record_heartbeat` |
| `0010_pause.sql` | The pause flag: the one-row `pause` table, and `set_pause` and `lift_pause` for the operator |
| `0011_seats_and_pause.sql` | The `seat` column on `claims`, with one running claim per seat; `claim_item` takes the seat (at the item's stage, an urgent seat for an urgent item only) and refuses while the factory is paused, except a `health` or a `digest` item (SQLSTATE `BRD05`); `set_pause` accepts the reconciler |

The `schema_migrations` table is created by the runner itself.
