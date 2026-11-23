# Migrations

Plain SQL, applied in order by `factory_board/migrate.py` and shipped in the image.

- Name a file `NNNN_<name>.sql`: four digits from `0001`, then lower-case letters, digits and `_`.
  No gaps and no duplicate numbers.
- Each file runs in one transaction with its `schema_migrations` row, so don't write
  `BEGIN`/`COMMIT`, and don't use statements that can't run in a transaction
  (`CREATE INDEX CONCURRENTLY`).
- Never edit, rename or delete a file that has been applied anywhere: the runner checks each
  applied file's SHA-256 and refuses to start. Fix a mistake with a new migration.

There are none yet. The `schema_migrations` table is created by the runner itself.
