-- No run writes a stage's item: a run (`run:<id>` at the agent tier) inserts only items on
-- the operator's list, and only the kinds its stage may write (a `deploy-check` run writes
-- a `decision`). The server's own check (`RUN_CREATES`) keeps to the second half; this
-- policy holds the first in the database too, should the server's check ever be wrong.
--
-- A stage's item is one whose assignee is not the operator (`items_kind_fits_assignee`
-- makes its kind the assignee): `assess`, `work`, `review`, `deploy-check`, `retro`,
-- `health`, `digest` and `merge`. Everyone else who writes one goes on as before: the
-- process tier (the reconciler), a session, and the board itself, whose
-- functions (`board_wake_assess`, the decision after two expiries) run as the owner, so a
-- run's close that wakes assess still writes the `assess` item.
--
-- Restrictive, like items_update_within_claim: it narrows items_insert_agent and grants
-- nothing. It reads the caller's identity, which items_insert_agent makes `created_by`.
-- The later PR that gives assess its tools loosens it for an `assess` run's `work` item,
-- the one the fix-forward limit (0009) counts.
CREATE POLICY items_insert_no_run_stage_item ON items
    AS RESTRICTIVE
    FOR INSERT TO board_agent
    WITH CHECK (
        assignee = 'operator'
        OR current_setting('board.actor', true) !~ '^run:'
    );
