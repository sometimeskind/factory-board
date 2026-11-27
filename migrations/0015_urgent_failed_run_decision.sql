-- The decision after two failed runs in a row is urgent, whatever the item is.
--
-- Until now it took the item's own `urgent` (0008, 0014), so for an ordinary item it sat in
-- the operator's list until they next looked: the item it stops is out of the queue, and
-- nothing said so. Urgent, it is pushed once it has been open for 15 minutes
-- (BoardUrgentItemWaiting). It matters more since a run that leaves no result is released
-- at once: the second failed run now comes a minute after the first, not 40.
--
-- Only the inserted `urgent` changes. The function keeps its owner, its arguments and its
-- grants (none: only `expire_claims` and `release_claim` call it, as the owner). A decision
-- written before this migration keeps the urgency it has.

CREATE OR REPLACE FUNCTION board_decide_after_failed_runs(failed public.items, holder text, actor text)
    RETURNS text
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    counted text[];
    told text[];
    new_id text;
    keyed text;
BEGIN
    SELECT array_agg(c.run ORDER BY c.claimed_at, c.run),
           array_agg(
               format(
                   'run %s %s',
                   c.run,
                   CASE WHEN c.ending = 'expired' THEN 'ran out of time'
                        ELSE 'left no result that could be accepted'
                   END
               )
               ORDER BY c.claimed_at, c.run
           )
        INTO counted, told
        FROM public.claims AS c
        WHERE c.item_id = failed.id
          AND c.decision_id IS NULL
          AND (c.ending = 'expired' OR (c.ending = 'released' AND c.reason = 'result-refused'));
    IF COALESCE(cardinality(counted), 0) < 2 THEN
        RETURN NULL;
    END IF;

    LOOP
        new_id := public.board_new_item_id();
        EXIT WHEN NOT EXISTS (SELECT FROM public.items AS i WHERE i.id = new_id);
    END LOOP;
    -- An item with no subject has only a key, and so does its decision.
    keyed := CASE WHEN failed.subject_type IS NULL THEN 'expired:' || failed.id END;
    INSERT INTO public.items (
        id, assignee, subject_repo, subject_type, subject_ref, key, kind, fields,
        claimed_by, urgent, created_by
    ) VALUES (
        new_id,
        'operator',
        failed.subject_repo,
        COALESCE(failed.subject_type, 'finding'),
        COALESCE(failed.subject_ref, keyed),
        keyed,
        'decision',
        jsonb_build_object(
            'kind', 'decision',
            'ticket', CASE WHEN failed.subject_type = 'issue' THEN
                jsonb_build_object(
                    'repo', failed.subject_repo, 'number', failed.subject_ref::bigint
                )
            END,
            'summary', format(
                'The %s item %s failed twice in a row (%s), so the pipeline does not start '
                'it again by itself. Only the first option starts it again; any other '
                'answer, a reply or a reject closes it.',
                failed.kind, failed.id, array_to_string(told, ', then ')
            ),
            'options', jsonb_build_array(
                'Try again: the item goes back in the queue, for two more runs',
                'Stop: close the item as superseded'
            )
        ),
        holder,
        true,
        actor
    );
    INSERT INTO public.item_after (item_id, after_id) VALUES (failed.id, new_id);
    UPDATE public.claims AS c
        SET decision_id = new_id
        WHERE c.run = ANY (counted);
    RETURN new_id;
END
$$;
