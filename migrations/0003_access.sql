-- Who may do what. The server logs in as board_app, which has no rights of its own, and
-- takes one tier per transaction (SET LOCAL ROLE) after setting board.actor to the
-- caller's identity. The policies below read both.
--
-- An UPDATE that a USING clause refuses is silent: it changes no row and raises nothing.
-- The store checks the row count.

GRANT USAGE ON SCHEMA public TO board_operator, board_agent, board_process;

-- No tier may delete or truncate. The columns that say what an item is (its ID, assignee,
-- subject, kind, key and provenance) are not in the UPDATE grant, so no tier can rewrite them.
GRANT SELECT, INSERT ON items TO board_operator, board_agent, board_process;
GRANT UPDATE (
    fields, claimed_by, claim_expires, not_before, urgent, result, closed_at, resolution,
    expiries, releases, reasons, input_hash
) ON items TO board_operator, board_agent, board_process;

GRANT SELECT ON replies TO board_operator, board_agent, board_process;
GRANT INSERT ON replies TO board_operator;

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
CREATE POLICY items_insert ON items
    FOR INSERT TO board_operator, board_agent, board_process
    WITH CHECK (created_by = current_setting('board.actor', true));

-- An item is closed by its claim holder. Each policy's USING clause is also its WITH CHECK,
-- so a tier can't move an item to a holder whose items it could not change.
CREATE POLICY items_update_operator ON items
    FOR UPDATE TO board_operator
    USING (true);
CREATE POLICY items_update_agent ON items
    FOR UPDATE TO board_agent
    USING (claimed_by IS NULL OR claimed_by !~ '^(human|process):');
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

-- The one way past the claim holder rule: whoever pushes to a PR again may move the head
-- SHA on its open pr-approve or pr-merge item, which someone else holds. It runs as the
-- owner and changes that one field and nothing else. True when an item was changed.
CREATE FUNCTION set_pr_head_sha(item text, head_sha text) RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    changed integer;
BEGIN
    IF head_sha IS NULL OR head_sha !~ '^[0-9a-f]{40}$' THEN
        RAISE EXCEPTION 'a head SHA is 40 lower-case hex characters'
            USING ERRCODE = 'check_violation';
    END IF;
    UPDATE public.items AS i
        SET fields = jsonb_set(i.fields, '{head_sha}', to_jsonb(set_pr_head_sha.head_sha))
        WHERE i.id = set_pr_head_sha.item
          AND i.closed_at IS NULL
          AND i.kind IN ('pr-approve', 'pr-merge');
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END
$$;

REVOKE ALL ON FUNCTION set_pr_head_sha(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION set_pr_head_sha(text, text) TO board_agent, board_process;
