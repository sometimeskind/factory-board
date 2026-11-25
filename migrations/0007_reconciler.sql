-- The reconciler: the script that reads GitHub and writes the items that follow from what
-- it finds. It is `process:reconciler` at the process tier. A stage's item is held by
-- nobody until a run claims it, so the holder rule alone would let the reconciler change
-- none of the items it writes. Two additions, both for that one identity.

-- It may close a stage's item that nobody holds: the fact the item stood for went away
-- (the PR merged, its head moved). The column grants still limit the update to the close.
-- An item a run holds is left to that run, and an item on the operator's list is covered
-- by the holder rule as before. The clause is also the WITH CHECK, so the update can't
-- hand the row to anyone.
CREATE POLICY items_update_reconciler ON items
    FOR UPDATE TO board_process
    USING (
        current_setting('board.actor', true) = 'process:reconciler'
        AND assignee <> 'operator'
        AND claimed_by IS NULL
    );

-- A second trigger on an issue whose `assess` item is still open merges into that item:
-- the earlier of the two dates wins (no date is the earliest), and the new reasons are
-- appended. An item a run holds is not touched: after its close the staleness hash decides.
--
-- A function and not a column grant: `not_before` and `reasons` stay closed to the process
-- tier, so the nightly can't hide an item it holds from the operator's list. It runs as the
-- owner and checks everything itself. Returns true when the item was open, unheld and an
-- `assess` item, and now carries the merge.
CREATE FUNCTION merge_assess_trigger(item text, wake timestamptz, triggers text[])
    RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text := current_setting('board.actor', true);
    trigger_name text;
BEGIN
    IF actor IS DISTINCT FROM 'process:reconciler' THEN
        RAISE EXCEPTION 'only the reconciler merges a trigger into an assess item'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF triggers IS NULL OR cardinality(triggers) > 20 THEN
        RAISE EXCEPTION 'at most 20 reasons, and not null' USING ERRCODE = 'check_violation';
    END IF;
    FOREACH trigger_name IN ARRAY triggers LOOP
        IF trigger_name IS NULL OR trigger_name !~ '^[a-z][a-z0-9-]{0,39}$' THEN
            RAISE EXCEPTION 'a reason is a short lower-case name'
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    UPDATE public.items AS i
        SET not_before = CASE
                WHEN i.not_before IS NULL OR merge_assess_trigger.wake IS NULL THEN NULL
                ELSE LEAST(i.not_before, merge_assess_trigger.wake)
            END,
            reasons = i.reasons || ARRAY(
                SELECT t.name
                FROM unnest(merge_assess_trigger.triggers) WITH ORDINALITY AS t (name, position)
                WHERE t.name <> ALL (i.reasons)
                GROUP BY t.name
                ORDER BY min(t.position)
            )
        WHERE i.id = merge_assess_trigger.item
          AND i.closed_at IS NULL
          AND i.kind = 'assess'
          AND i.claimed_by IS NULL;
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION merge_assess_trigger(text, timestamptz, text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION merge_assess_trigger(text, timestamptz, text[]) TO board_process;
