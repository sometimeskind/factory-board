-- Who may do what. The server logs in as board_app, which has no rights of its own, and
-- takes one tier per transaction (SET LOCAL ROLE) after setting board.actor to the
-- caller's identity. The policies below read both.
--
-- An UPDATE that a USING clause refuses is silent: it changes no row and raises nothing.
-- The store checks the row count.

GRANT USAGE ON SCHEMA public TO board_operator, board_agent, board_process;

-- No tier may delete or truncate, and the grants are by column.
--
-- INSERT: a new item is open and has no history. The close (closed_at, resolution, result),
-- the counters, claim_expires, head_sha_moves and created_at are not in the grant, so no
-- tier can insert an item that looks answered or one with a past.
GRANT SELECT ON items TO board_operator, board_agent, board_process;
GRANT INSERT (
    id, assignee, subject_repo, subject_type, subject_ref, kind, fields, machine, claimed_by,
    not_before, urgent, key, reasons, input_hash, created_by, created_run, operator_typed
) ON items TO board_operator, board_agent, board_process;

-- UPDATE: only what the store writes. Every tier may close; deferring and bumping are the
-- operator's. No tier updates `fields`: an item's command never changes after it is
-- written, and set_pr_head_sha below is the only way to its one moving part. The claim
-- columns, the counters, reasons and input_hash get their grants with the build step that
-- writes them. What an item is (its ID, assignee, subject, kind, key, provenance) is never
-- granted.
GRANT UPDATE (closed_at, resolution, result) ON items
    TO board_operator, board_agent, board_process;
GRANT UPDATE (not_before, urgent) ON items TO board_operator;

GRANT SELECT ON replies TO board_operator, board_agent, board_process;
GRANT INSERT (item_id, author, body) ON replies TO board_operator;

-- FORCE makes the policies apply to the tables' owner too, so the owner's access is the
-- policy written here and not an unstated bypass.
ALTER TABLE items ENABLE ROW LEVEL SECURITY;
ALTER TABLE items FORCE ROW LEVEL SECURITY;
ALTER TABLE replies ENABLE ROW LEVEL SECURITY;
ALTER TABLE replies FORCE ROW LEVEL SECURITY;

-- The owner: the migrations, repairs by hand, and the function below.
CREATE POLICY items_owner ON items
    FOR ALL TO CURRENT_USER
    USING (true) WITH CHECK (true);
CREATE POLICY replies_owner ON replies
    FOR ALL TO CURRENT_USER
    USING (true) WITH CHECK (true);

-- Every tier reads every item, and creates items under its own identity. An agent may
-- create an item that the operator holds: that is how a session hands a step over.
CREATE POLICY items_select ON items
    FOR SELECT TO board_operator, board_agent, board_process
    USING (true);
-- A new item is open, with no result and no counted claims, whatever the grants say.
-- operator_typed (the operator typed into the run that wrote the item) is asserted by the
-- caller, as a session's identity is: an agent's word is taken for it. A process has no
-- operator at its keyboard and may not claim one.
CREATE POLICY items_insert_operator ON items
    FOR INSERT TO board_operator
    WITH CHECK (
        created_by = current_setting('board.actor', true)
        AND closed_at IS NULL AND resolution IS NULL AND result IS NULL
        AND expiries = 0 AND releases = 0 AND head_sha_moves = '[]'
    );
-- An agent's identity is session:<id> or run:<id>, and created_run is that ID, so an item
-- can't be attributed to another run.
CREATE POLICY items_insert_agent ON items
    FOR INSERT TO board_agent
    WITH CHECK (
        created_by = current_setting('board.actor', true)
        AND created_by ~ '^(session|run):'
        AND created_run = split_part(created_by, ':', 2)
        AND closed_at IS NULL AND resolution IS NULL AND result IS NULL
        AND expiries = 0 AND releases = 0 AND head_sha_moves = '[]'
    );
CREATE POLICY items_insert_process ON items
    FOR INSERT TO board_process
    WITH CHECK (
        created_by = current_setting('board.actor', true)
        AND NOT operator_typed
        AND closed_at IS NULL AND resolution IS NULL AND result IS NULL
        AND expiries = 0 AND releases = 0 AND head_sha_moves = '[]'
    );

-- An item is closed by its claim holder. The agent and process tiers may update only a row
-- they hold themselves; for an agent the prefix test stays as a second guard, should
-- board.actor ever be wrong. A row nobody holds is updatable by the operator only: claiming
-- comes with a later build step. Each policy's USING clause is also its WITH CHECK, so an
-- update can't hand the row to someone else.
CREATE POLICY items_update_operator ON items
    FOR UPDATE TO board_operator
    USING (true);
CREATE POLICY items_update_agent ON items
    FOR UPDATE TO board_agent
    USING (
        claimed_by = current_setting('board.actor', true)
        AND claimed_by !~ '^(human|process):'
    );
CREATE POLICY items_update_process ON items
    FOR UPDATE TO board_process
    USING (claimed_by = current_setting('board.actor', true));

-- Every tier reads the replies; only the operator writes one, under their own identity.
CREATE POLICY replies_select ON replies
    FOR SELECT TO board_operator, board_agent, board_process
    USING (true);
CREATE POLICY replies_insert ON replies
    FOR INSERT TO board_operator
    WITH CHECK (author = current_setting('board.actor', true));

-- The one way past the claim holder rule, and the only way to change anything in `fields`:
-- whoever pushes to a PR again may move the head SHA on its open pr-approve or pr-merge
-- item, which someone else holds. It runs as the owner, so it checks everything itself.
--
-- The caller names the PR as well as the item; an ID that belongs to another PR's item
-- changes nothing. Every move is appended to head_sha_moves with the old SHA, the caller's
-- identity and the time, so the operator sees on the item that it moved. Returns true when
-- the item now carries the SHA, false when no open PR item of that ID is on that PR.
CREATE FUNCTION set_pr_head_sha(item text, repo text, pr text, head_sha text) RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text := current_setting('board.actor', true);
    current_sha text;
BEGIN
    IF actor IS NULL OR actor !~ '^(human|process|run|session):[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
    THEN
        RAISE EXCEPTION 'board.actor is not set to an identity'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF head_sha IS NULL OR head_sha !~ '^[0-9a-f]{40}$' THEN
        RAISE EXCEPTION 'a head SHA is 40 lower-case hex characters'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT i.fields ->> 'head_sha' INTO current_sha
        FROM public.items AS i
        WHERE i.id = set_pr_head_sha.item
          AND i.closed_at IS NULL
          AND i.kind IN ('pr-approve', 'pr-merge')
          AND i.subject_type = 'pr'
          AND i.subject_repo = set_pr_head_sha.repo
          AND i.subject_ref = set_pr_head_sha.pr
        FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF current_sha IS NOT DISTINCT FROM head_sha THEN
        RETURN true;
    END IF;
    UPDATE public.items AS i
        SET fields = jsonb_set(i.fields, '{head_sha}', to_jsonb(set_pr_head_sha.head_sha)),
            head_sha_moves = i.head_sha_moves || jsonb_build_object(
                'from', current_sha,
                'to', set_pr_head_sha.head_sha,
                'by', actor,
                'at', now()
            )
        WHERE i.id = set_pr_head_sha.item;
    RETURN true;
END
$$;

REVOKE ALL ON FUNCTION set_pr_head_sha(text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION set_pr_head_sha(text, text, text, text) TO board_agent, board_process;
