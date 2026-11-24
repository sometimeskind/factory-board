-- Dependencies between items, and whether an operator's item holds its issue.
--
-- An item may be created `after` other items: it waits while any of them is open. Waiting
-- is not stored. It is read from the edges below and the other items' `closed_at`, the way
-- "deferred" is read from `not_before`.

-- Whether an item for the operator holds its issue: the issue waits on the operator because
-- of this item. Nothing reads it yet; a later build step does. Only an operator's item on an
-- issue can say it does not, and no tier may change it after the item is written.
ALTER TABLE items
    ADD COLUMN holds boolean NOT NULL DEFAULT true
    CONSTRAINT items_holds_false_on_operator_issue
        CHECK (holds OR (assignee = 'operator' AND subject_type IS NOT DISTINCT FROM 'issue'));

GRANT INSERT (holds) ON items TO board_operator, board_agent, board_process;

-- One row per edge: `item_id` waits on `after_id`. An edge is written with its item and
-- never afterwards, and it points at an item that was already there, so there are no cycles.
CREATE TABLE item_after (
    item_id text NOT NULL REFERENCES items (id),
    after_id text NOT NULL REFERENCES items (id),
    PRIMARY KEY (item_id, after_id),
    CONSTRAINT item_after_not_itself CHECK (item_id <> after_id)
);

-- What a close reads: the items that wait on this one.
CREATE INDEX item_after_by_after ON item_after (after_id);

-- No UPDATE and no DELETE for any tier: an edge is as final as the item's subject.
GRANT SELECT, INSERT ON item_after TO board_operator, board_agent, board_process;

ALTER TABLE item_after ENABLE ROW LEVEL SECURITY;
ALTER TABLE item_after FORCE ROW LEVEL SECURITY;

CREATE POLICY item_after_owner ON item_after
    FOR ALL TO CURRENT_USER
    USING (true) WITH CHECK (true);
CREATE POLICY item_after_select ON item_after
    FOR SELECT TO board_operator, board_agent, board_process
    USING (true);
-- A tier adds an edge only to an item it wrote itself in this same transaction. No tier
-- can insert `created_at`, so it is always the default, now(), which is the transaction's
-- start: an item from an earlier transaction never matches. Without this, any tier could
-- make someone else's item wait, or make its own wait after the operator had read it.
CREATE POLICY item_after_insert ON item_after
    FOR INSERT TO board_operator, board_agent, board_process
    WITH CHECK (
        EXISTS (
            SELECT FROM items AS i
            WHERE i.id = item_after.item_id
              AND i.created_by = current_setting('board.actor', true)
              AND i.created_at = transaction_timestamp()
        )
    );

-- Success is required. An item closed as done, answered or superseded lets go of what waits
-- on it; one closed as rejected or replied does not, and nothing may be made to wait on it.
--
-- The lock is taken here and in the cascade below, so the two never run side by side: a
-- new edge either is committed before a cascade looks for it, or sees the close that
-- started the cascade. Every statement in these functions reads what is committed by then.
CREATE FUNCTION item_after_refuse_failed() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    failed text;
BEGIN
    PERFORM pg_advisory_xact_lock(5, 1);
    -- An item the cascade closed counts as failed too: what it waited on was refused.
    SELECT CASE WHEN i.resolution = 'superseded' THEN 'superseded by a cascade'
                ELSE i.resolution END
        INTO failed
        FROM public.items AS i
        WHERE i.id = NEW.after_id
          AND (
              i.resolution IN ('rejected', 'replied')
              OR (i.resolution = 'superseded' AND i.result ->> 'cause' = 'after')
          );
    IF FOUND THEN
        RAISE EXCEPTION 'item % was closed as %, so nothing can wait on it', NEW.after_id, failed
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER item_after_not_after_failed
    BEFORE INSERT ON item_after
    FOR EACH ROW
    EXECUTE FUNCTION item_after_refuse_failed();

-- A waiting item can't be closed as done or answered, by anyone: what it waits on has not
-- happened. It can still be rejected, replied to or superseded. Runs as the caller, who may
-- read both tables.
CREATE FUNCTION items_refuse_done_while_waiting() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    waiting_on text;
BEGIN
    SELECT string_agg(a.after_id, ', ' ORDER BY a.after_id) INTO waiting_on
        FROM public.item_after AS a
        JOIN public.items AS before ON before.id = a.after_id
        WHERE a.item_id = OLD.id AND before.closed_at IS NULL;
    IF waiting_on IS NOT NULL THEN
        -- BRD02: the store reads this code as "the item is waiting".
        RAISE EXCEPTION 'item % waits on % and cannot be closed as % while that is open',
            OLD.id, waiting_on, NEW.resolution
            USING ERRCODE = 'BRD02';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER items_waiting_is_not_done
    BEFORE UPDATE ON items
    FOR EACH ROW
    WHEN (OLD.closed_at IS NULL AND NEW.resolution IN ('done', 'answered'))
    EXECUTE FUNCTION items_refuse_done_while_waiting();

-- Closing an item as rejected or replied closes every open item that waits on it, and what
-- waits on those, all the way down, as superseded. The whole chain goes in one statement: a
-- step at a time, the first cascaded `superseded` would let go of the next item down.
--
-- It runs as the owner. The items down the chain may be held by anyone, and the caller's
-- tier could close none of them; closing them is the board's act, not the caller's. The
-- result says so: the item whose close started it, and how that item closed.
--
-- The cascade closes as superseded, so it fires neither this trigger again nor the one
-- above.
CREATE FUNCTION items_cascade_close() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(5, 1);
    WITH RECURSIVE waiting (id) AS (
        SELECT a.item_id
            FROM public.item_after AS a
            JOIN public.items AS i ON i.id = a.item_id
            WHERE a.after_id = NEW.id AND i.closed_at IS NULL
        UNION
        SELECT a.item_id
            FROM waiting AS w
            JOIN public.item_after AS a ON a.after_id = w.id
            JOIN public.items AS i ON i.id = a.item_id
            WHERE i.closed_at IS NULL
    )
    UPDATE public.items AS i
        SET closed_at = now(),
            resolution = 'superseded',
            result = jsonb_build_object(
                'cause', 'after',
                'item', NEW.id,
                'resolution', NEW.resolution
            )
        FROM waiting AS w
        WHERE i.id = w.id AND i.closed_at IS NULL;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION items_cascade_close() FROM PUBLIC;

CREATE TRIGGER items_close_cascades
    AFTER UPDATE ON items
    FOR EACH ROW
    WHEN (OLD.closed_at IS NULL AND NEW.resolution IN ('rejected', 'replied'))
    EXECUTE FUNCTION items_cascade_close();
