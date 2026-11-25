-- The pause flag: the operator stops the factory from starting work, and lifts the stop.
--
-- The flag is one row: there while the factory is paused, gone when it is not. The board
-- only keeps it. The dispatcher reads it with the queue and starts nothing but `health` and
-- `digest` while it stands; nothing here refuses a claim.

CREATE TABLE pause (
    -- One row at most: the key can only be true.
    only_row boolean PRIMARY KEY DEFAULT true
        CONSTRAINT pause_one_row CHECK (only_row),
    paused_at timestamptz NOT NULL,
    paused_by text NOT NULL
        CONSTRAINT pause_paused_by_format
            CHECK (paused_by ~ '^human:[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    reason text NOT NULL
        CONSTRAINT pause_reason_length CHECK (char_length(reason) BETWEEN 1 AND 500)
);

ALTER TABLE pause ENABLE ROW LEVEL SECURITY;
ALTER TABLE pause FORCE ROW LEVEL SECURITY;

CREATE POLICY pause_owner ON pause
    FOR ALL TO CURRENT_USER
    USING (true) WITH CHECK (true);

-- Every tier reads it, and none writes it: the two functions below do.
GRANT SELECT ON pause TO board_operator, board_agent, board_process;
CREATE POLICY pause_select ON pause
    FOR SELECT TO board_operator, board_agent, board_process
    USING (true);

-- Pause, at the database's own time. The operator only. A pause that already stands is
-- kept as it is, with its own time and reason: `is_new` says which of the two happened.
CREATE FUNCTION set_pause(why text)
    RETURNS TABLE (paused_at timestamptz, paused_by text, reason text, is_new boolean)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
#variable_conflict use_column
DECLARE
    actor text := current_setting('board.actor', true);
    written boolean;
BEGIN
    IF actor IS NULL OR actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._-]{0,99}$' THEN
        RAISE EXCEPTION 'only the operator pauses the factory'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO public.pause (paused_at, paused_by, reason)
        VALUES (clock_timestamp(), actor, why)
        ON CONFLICT (only_row) DO NOTHING;
    written := FOUND;
    RETURN QUERY
        SELECT p.paused_at, p.paused_by, p.reason, written FROM public.pause AS p;
END
$$;

REVOKE ALL ON FUNCTION set_pause(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION set_pause(text) TO board_operator;

-- Lift the pause. The operator only. Returns the pause that stood, or no row when the
-- factory was not paused.
CREATE FUNCTION lift_pause()
    RETURNS TABLE (paused_at timestamptz, paused_by text, reason text)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
#variable_conflict use_column
DECLARE
    actor text := current_setting('board.actor', true);
BEGIN
    IF actor IS NULL OR actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._-]{0,99}$' THEN
        RAISE EXCEPTION 'only the operator lifts the pause'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN QUERY
        DELETE FROM public.pause AS p RETURNING p.paused_at, p.paused_by, p.reason;
END
$$;

REVOKE ALL ON FUNCTION lift_pause() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lift_pause() TO board_operator;
