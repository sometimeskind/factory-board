-- A run whose result can't be accepted is released as `result-refused`, and that release
-- counts toward the decision item the way an expiry does (0008).
--
-- The runner releases with it when the board refused the run's result on close, when the
-- result failed the schema check before it was sent, or when what the run left could not be
-- collected. As `stopped` it counted nowhere, so a run that keeps leaving a bad result was
-- started again forever. Now two failed runs in a row, any mix of expiries and refused
-- results, write the decision, under 0008's rule for "in a row": a failed run is a claim
-- that expired or was released as `result-refused` and is in no decision yet, and any other
-- release in between neither counts nor starts the row again.
--
-- A `result-refused` release is still a release: the item is back in the queue at once (or
-- waits on the decision), `releases` goes up and `expiries` does not. Every other reason
-- counts nowhere, as before.

-- ---- A refused result is reported in the decision like an expiry ----

ALTER TABLE claims DROP CONSTRAINT claims_decision_on_expiry;
ALTER TABLE claims ADD CONSTRAINT claims_decision_on_failed_run
    CHECK (
        decision_id IS NULL
        OR ending IS NOT DISTINCT FROM 'expired'
        OR (ending IS NOT DISTINCT FROM 'released' AND reason IS NOT DISTINCT FROM 'result-refused')
    );

-- ---- The decision, for the expiry and for the release ----

-- 0008's decision, taken out of `expire_claims` so the release writes the same one: when the
-- item's failed runs that are in no decision yet are two, the board writes the decision for
-- the operator, makes the item wait on it, and names it on both claims. Returns its ID, or
-- null when no decision is due. The summary says how each run failed. Only the two functions
-- below call it, as the owner; no tier may.
CREATE FUNCTION board_decide_after_failed_runs(failed public.items, holder text, actor text)
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
        failed.urgent,
        actor
    );
    INSERT INTO public.item_after (item_id, after_id) VALUES (failed.id, new_id);
    UPDATE public.claims AS c
        SET decision_id = new_id
        WHERE c.run = ANY (counted);
    RETURN new_id;
END
$$;

REVOKE ALL ON FUNCTION board_decide_after_failed_runs(public.items, text, text) FROM PUBLIC;

-- ---- The expiry ----

-- As in 0008, with the decision written by the function above.
CREATE OR REPLACE FUNCTION expire_claims(holder text)
    RETURNS TABLE (expired_item text, expired_run text, decision text)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text := current_setting('board.actor', true);
    expired public.items%ROWTYPE;
    ended_run text;
    new_id text;
BEGIN
    IF actor IS DISTINCT FROM 'process:dispatcher' THEN
        RAISE EXCEPTION 'only the dispatcher counts expired claims'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF holder IS NULL OR holder !~ '^human:[A-Za-z0-9][A-Za-z0-9._-]{0,99}$' THEN
        RAISE EXCEPTION 'the decision is held by a person' USING ERRCODE = 'check_violation';
    END IF;

    FOR expired IN
        SELECT i.*
            FROM public.items AS i
            WHERE i.closed_at IS NULL AND i.claimed_by ~ '^run:' AND i.claim_expires <= now()
            ORDER BY i.claim_expires, i.id
            FOR UPDATE
    LOOP
        UPDATE public.items AS i
            SET claimed_by = NULL, claim_expires = NULL, expiries = i.expiries + 1
            WHERE i.id = expired.id;
        ended_run := NULL;
        UPDATE public.claims AS c
            SET ended_at = now(), ending = 'expired'
            WHERE c.item_id = expired.id AND c.ended_at IS NULL
            RETURNING c.run INTO ended_run;
        new_id := public.board_decide_after_failed_runs(expired, expire_claims.holder, actor);

        expired_item := expired.id;
        expired_run := COALESCE(ended_run, split_part(expired.claimed_by, ':', 2));
        decision := new_id;
        RETURN NEXT;
    END LOOP;
END
$$;

-- ---- The release ----

-- As in 0008, with `holder`: a `result-refused` release is a failed run, so it may write the
-- decision, and the decision is held by the person the server names (the database does not
-- know the operator's name). The other reasons take no holder and write nothing; a holder
-- given with one is still checked. The argument list changed, so the old function goes;
-- `holder` defaults to null, so a call with three arguments still finds this one.
DROP FUNCTION release_claim(text, text, timestamptz);

CREATE FUNCTION release_claim(item text, reason text, wake timestamptz, holder text DEFAULT NULL)
    RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text := current_setting('board.actor', true);
    found_item public.items%ROWTYPE;
BEGIN
    IF actor IS NULL
        OR (actor <> 'process:dispatcher' AND actor !~ '^run:[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
    THEN
        RAISE EXCEPTION 'only the dispatcher or the run that holds a claim releases it'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF reason IS NULL OR reason !~ '^[a-z][a-z0-9-]{0,39}$' THEN
        RAISE EXCEPTION 'a reason is a short lower-case name' USING ERRCODE = 'check_violation';
    END IF;
    IF (holder IS NOT NULL OR reason = 'result-refused')
        AND (holder IS NULL OR holder !~ '^human:[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
    THEN
        RAISE EXCEPTION 'the decision is held by a person' USING ERRCODE = 'check_violation';
    END IF;

    SELECT * INTO found_item
        FROM public.items AS i
        WHERE i.id = release_claim.item
        FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF found_item.closed_at IS NOT NULL THEN
        RAISE EXCEPTION 'item % is closed and cannot be changed', item USING ERRCODE = 'BRD01';
    END IF;
    IF found_item.claimed_by IS NULL OR found_item.claimed_by !~ '^run:' THEN
        RAISE EXCEPTION 'item % is held by %, not by a run',
            item, COALESCE(found_item.claimed_by, 'nobody')
            USING ERRCODE = 'BRD03';
    END IF;
    IF actor <> 'process:dispatcher' AND actor <> found_item.claimed_by THEN
        RAISE EXCEPTION 'item % is held by another run', item
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF found_item.claim_expires <= now() THEN
        RAISE EXCEPTION 'the claim on item % has run out; it can only expire', item
            USING ERRCODE = 'BRD03';
    END IF;
    IF wake IS NOT NULL THEN
        IF found_item.kind <> 'deploy-check' THEN
            RAISE EXCEPTION 'only a deploy-check is released until a later time'
                USING ERRCODE = 'check_violation';
        END IF;
        IF wake <= now() OR wake > now() + interval '24 hours' THEN
            RAISE EXCEPTION 'a release until a later time is for at most a day ahead'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    UPDATE public.items AS i
        SET claimed_by = NULL,
            claim_expires = NULL,
            releases = i.releases + 1,
            not_before = COALESCE(release_claim.wake, i.not_before)
        WHERE i.id = release_claim.item;
    UPDATE public.claims AS c
        SET ended_at = now(), ending = 'released', reason = release_claim.reason
        WHERE c.item_id = release_claim.item AND c.ended_at IS NULL;
    IF release_claim.reason = 'result-refused' THEN
        PERFORM public.board_decide_after_failed_runs(found_item, release_claim.holder, actor);
    END IF;
    RETURN true;
END
$$;

REVOKE ALL ON FUNCTION release_claim(text, text, timestamptz, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION release_claim(text, text, timestamptz, text)
    TO board_agent, board_process;
