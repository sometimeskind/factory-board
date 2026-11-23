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
- The migrations run as the tables' owner. `items` and `replies` are under `FORCE ROW LEVEL
  SECURITY`, so the owner reads and writes them through the `*_owner` policies; a new table
  under row security needs one too, or a later migration's `UPDATE` on it changes no row and
  says nothing.

| File | Holds |
|---|---|
| `0001_items.sql` | The role check; the `items` table with its checks, the three uniqueness rules, the list index, and the trigger that makes a closed item final |
| `0002_replies.sql` | The `replies` table |
| `0003_access.sql` | The grants, row-level security and its policies, and `set_pr_head_sha` |

The `schema_migrations` table is created by the runner itself.
