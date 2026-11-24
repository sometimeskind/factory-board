-- Claims: the dispatcher starts every run, and a stage's item is claimed for that one run.
--
-- The dispatcher is `process:dispatcher` at the process tier. It gets no new right on a row:
-- everything here is a function that runs as the owner and checks the caller itself, so the
-- claim columns and the counters stay closed to every tier.

-- One row per claim, kept after the claim ends: who ran what, until when, and how it ended.
-- A claim ends in one of three ways: the item `closed` under it, the claim was `released`
-- (a known interruption, with its reason), or it `expired` (the run crashed or hit its cap).
CREATE TABLE claims (
    -- The run's ID, made by the dispatcher. A run claims once: an ID is never used again,
    -- so `run:<id>` on an item always means this one claim.
    run text PRIMARY KEY
        CONSTRAINT claims_run_format CHECK (run ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    item_id text NOT NULL REFERENCES items (id),
    -- The SHA-256 of the run's token. The token itself is returned once, by the claim, and
    -- stored nowhere.
    token_sha256 text NOT NULL
        CONSTRAINT claims_token_format CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    claimed_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    ended_at timestamptz,
    ending text
        CONSTRAINT claims_ending_allowed CHECK (ending IN ('closed', 'released', 'expired')),
    -- Why a claim was released, as a short name.
    reason text
        CONSTRAINT claims_reason_format CHECK (reason ~ '^[a-z][a-z0-9-]{0,39}$'),
    -- The decision item this expiry was reported in. An expiry with none still counts
    -- toward the next one.
    decision_id text REFERENCES items (id),
    CONSTRAINT claims_token_once UNIQUE (token_sha256),
    CONSTRAINT claims_ended_with_ending CHECK ((ended_at IS NULL) = (ending IS NULL)),
    CONSTRAINT claims_reason_on_release
        CHECK ((reason IS NOT NULL) = (ending IS NOT DISTINCT FROM 'released')),
    CONSTRAINT claims_decision_on_expiry
        CHECK (decision_id IS NULL OR ending IS NOT DISTINCT FROM 'expired')
);

-- At most one running claim per item.
CREATE UNIQUE INDEX claims_running ON claims (item_id) WHERE ended_at IS NULL;
-- An item's history, and the last claims of all.
CREATE INDEX claims_by_item ON claims (item_id, claimed_at);
CREATE INDEX claims_by_time ON claims (claimed_at);

ALTER TABLE claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE claims FORCE ROW LEVEL SECURITY;

CREATE POLICY claims_owner ON claims
    FOR ALL TO CURRENT_USER
    USING (true) WITH CHECK (true);

-- Every tier reads the history, and no tier reads a token's hash or writes a row.
GRANT SELECT (run, item_id, claimed_at, expires_at, ended_at, ending, reason, decision_id)
    ON claims TO board_operator, board_agent, board_process;
CREATE POLICY claims_select ON claims
    FOR SELECT TO board_operator, board_agent, board_process
    USING (true);

-- What a run may be started on, and in which order: an open item of a stage that nobody
-- holds, whose date has come and that waits on no open item. `merge` items are left out:
-- the merge script takes them in the tick, with no claim.
--
-- The order is the rank, then the oldest item: anything urgent, then `health`, `digest`,
-- `review`, `deploy-check`, `assess`, `work`, `retro`. Later stages go first, so one `work`
-- item's chain drains before the next `work` item starts.
--
-- It reads as the caller (security_invoker), who may read every item anyway.
CREATE VIEW claimable WITH (security_invoker = true) AS
    SELECT i.id,
           CASE
               WHEN i.urgent THEN 1
               ELSE CASE i.kind
                   WHEN 'health' THEN 2
                   WHEN 'digest' THEN 3
                   WHEN 'review' THEN 4
                   WHEN 'deploy-check' THEN 5
                   WHEN 'assess' THEN 6
                   WHEN 'work' THEN 7
                   WHEN 'retro' THEN 8
               END
           END AS rank
    FROM items AS i
    WHERE i.closed_at IS NULL
      AND i.assignee NOT IN ('operator', 'merge')
      AND i.claimed_by IS NULL
      AND (i.not_before IS NULL OR i.not_before <= now())
      AND NOT EXISTS (
          SELECT FROM item_after AS a
          JOIN items AS before ON before.id = a.after_id
          WHERE a.item_id = i.id AND before.closed_at IS NULL
      );

GRANT SELECT ON claimable TO board_operator, board_agent, board_process;

-- A claim that has run out can only expire: the run that held it closes nothing any more,
-- even before the dispatcher has counted the expiry. A session's claim has no expiry.
CREATE POLICY items_update_within_claim ON items
    AS RESTRICTIVE
    FOR UPDATE TO board_agent
    USING (claim_expires IS NULL OR claim_expires > now());

-- An item ID as the store makes one: eight characters of its alphabet, from random bytes
-- (the six of a version 4 UUID before its version, and two after its variant). For the
-- item the board writes itself, below.
CREATE FUNCTION board_new_item_id() RETURNS text
    LANGUAGE sql
    VOLATILE
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT string_agg(
               substr('abcdefghjkmnpqrstuvwxyz23456789', 1 + get_byte(random.bytes, n) % 31, 1),
               ''
           )
    FROM (SELECT uuid_send(gen_random_uuid()) AS bytes) AS random,
         unnest(ARRAY[0, 1, 2, 3, 4, 5, 10, 11]) AS n
$$;

REVOKE ALL ON FUNCTION board_new_item_id() FROM PUBLIC;

-- The claim: one item for one run, in one statement's worth of locking. The dispatcher only.
--
-- It refuses an item that is not in `claimable` (BRD03: the store reads this code as "not
-- claimable"), and a run ID that has claimed before. The claim runs out ten minutes after
-- the stage's time cap, which the dispatcher passes in minutes.
--
-- The staleness hash is the dispatcher's to bring, from the reconciler's last pass:
--   - an `assess` item is stamped with it (an item on an issue needs one);
--   - a `work` item that carries the hash it was assessed under is compared with it, and on
--     a difference it is closed as superseded and nothing is claimed ('stale');
--   - no other item takes one.
-- Returns 'claimed' or 'stale'.
CREATE FUNCTION claim_item(
    item text, run text, cap_minutes integer, token_sha256 text, current_hash text
) RETURNS text
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text := current_setting('board.actor', true);
    found_item public.items%ROWTYPE;
BEGIN
    IF actor IS DISTINCT FROM 'process:dispatcher' THEN
        RAISE EXCEPTION 'only the dispatcher claims an item for a run'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF run IS NULL OR run !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$' THEN
        RAISE EXCEPTION 'a run ID is letters, digits, ''.'', ''_'' or ''-'''
            USING ERRCODE = 'check_violation';
    END IF;
    IF token_sha256 IS NULL OR token_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'a token hash is 64 lower-case hex characters'
            USING ERRCODE = 'check_violation';
    END IF;
    IF cap_minutes IS NULL OR cap_minutes NOT BETWEEN 1 AND 1440 THEN
        RAISE EXCEPTION 'a time cap is 1 to 1440 minutes' USING ERRCODE = 'check_violation';
    END IF;
    IF current_hash IS NOT NULL AND current_hash !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'a staleness hash is up to 128 letters, digits, ''.'', ''_'', '':'' or ''-'''
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT * INTO found_item
        FROM public.items AS i
        WHERE i.id = claim_item.item
        FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no item % to claim', item USING ERRCODE = 'BRD03';
    END IF;
    IF found_item.closed_at IS NOT NULL THEN
        RAISE EXCEPTION 'item % is closed and cannot be changed', item USING ERRCODE = 'BRD01';
    END IF;
    -- Read after the lock, so it sees whatever was committed while this waited for the row.
    IF NOT EXISTS (SELECT FROM public.claimable AS c WHERE c.id = claim_item.item) THEN
        RAISE EXCEPTION
            'item % is not claimable: it is held by %, deferred, waiting, or not a stage''s item',
            item, COALESCE(found_item.claimed_by, 'nobody')
            USING ERRCODE = 'BRD03';
    END IF;
    IF EXISTS (SELECT FROM public.claims AS c WHERE c.run = claim_item.run) THEN
        RAISE EXCEPTION 'run % has claimed before; a run claims once', run
            USING ERRCODE = 'check_violation';
    END IF;

    IF found_item.kind = 'assess' THEN
        IF current_hash IS NULL AND found_item.subject_type = 'issue' THEN
            RAISE EXCEPTION 'an assess item on an issue is claimed with the issue''s hash'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF found_item.kind = 'work' AND found_item.input_hash IS NOT NULL THEN
        IF current_hash IS NULL THEN
            RAISE EXCEPTION 'a work item that carries a hash is claimed with the issue''s hash'
                USING ERRCODE = 'check_violation';
        END IF;
        IF current_hash <> found_item.input_hash THEN
            UPDATE public.items AS i
                SET closed_at = now(),
                    resolution = 'superseded',
                    result = jsonb_build_object('cause', 'stale', 'input_hash', current_hash)
                WHERE i.id = claim_item.item;
            RETURN 'stale';
        END IF;
    ELSIF current_hash IS NOT NULL THEN
        RAISE EXCEPTION 'only an assess item, or a work item that carries a hash, takes one'
            USING ERRCODE = 'check_violation';
    END IF;

    UPDATE public.items AS i
        SET claimed_by = 'run:' || claim_item.run,
            claim_expires = now() + make_interval(mins => claim_item.cap_minutes + 10),
            input_hash = CASE
                WHEN i.kind = 'assess' THEN COALESCE(claim_item.current_hash, i.input_hash)
                ELSE i.input_hash
            END
        WHERE i.id = claim_item.item;
    INSERT INTO public.claims (run, item_id, token_sha256, expires_at)
        VALUES (
            claim_item.run,
            claim_item.item,
            claim_item.token_sha256,
            now() + make_interval(mins => claim_item.cap_minutes + 10)
        );
    RETURN 'claimed';
END
$$;

REVOKE ALL ON FUNCTION claim_item(text, text, integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_item(text, text, integer, text, text) TO board_process;

-- The release: a known interruption gives the item back to the queue at once. It is counted
-- on the item and kept with its reason on the claim, and it does not count as an expiry.
--
-- The dispatcher releases any run's claim (the unit did not start); a run releases only its
-- own. A claim that has run out is not released: it can only expire. `wake` is for a
-- `deploy-check` that looked and found the change not live yet: the item returns to the
-- queue at that time, at most a day ahead. Returns false when there is no such item.
CREATE FUNCTION release_claim(item text, reason text, wake timestamptz) RETURNS boolean
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
    RETURN true;
END
$$;

REVOKE ALL ON FUNCTION release_claim(text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION release_claim(text, text, timestamptz) TO board_agent, board_process;

-- Expiry: nothing in the database runs by itself, so the dispatcher counts the claims that
-- ran out, each tick, before it reads the queue. An expired claim leaves the item open and
-- unclaimed, with one more in `expiries`.
--
-- Two expired claims in a row on one item write a `decision` item for the operator, and
-- the pipeline does not try a third time by itself. A released claim in between neither
-- counts nor starts the row again. The decision:
--   - is about what the item is about, is urgent if the item is, and is held by `holder`
--     (the operator, whose name the server knows and the database does not);
--   - is what the item now waits on, so it is in no queue while the decision is open. This
--     is the one edge written after its item, and the board writes it, not a tier;
--   - lets the item go only when its first option is chosen: two more runs. Any other
--     close of it (the second option, the operator's own text, a reject, a reply) closes
--     the item as superseded.
-- Returns one row per expired claim, with the decision item if one was written.
CREATE FUNCTION expire_claims(holder text)
    RETURNS TABLE (expired_item text, expired_run text, decision text)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text := current_setting('board.actor', true);
    expired public.items%ROWTYPE;
    ended_run text;
    uncounted text[];
    new_id text;
    keyed text;
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
        SELECT array_agg(c.run ORDER BY c.claimed_at, c.run) INTO uncounted
            FROM public.claims AS c
            WHERE c.item_id = expired.id AND c.ending = 'expired' AND c.decision_id IS NULL;

        new_id := NULL;
        IF cardinality(uncounted) >= 2 THEN
            LOOP
                new_id := public.board_new_item_id();
                EXIT WHEN NOT EXISTS (SELECT FROM public.items AS i WHERE i.id = new_id);
            END LOOP;
            -- An item with no subject has only a key, and so does its decision.
            keyed := CASE WHEN expired.subject_type IS NULL THEN 'expired:' || expired.id END;
            INSERT INTO public.items (
                id, assignee, subject_repo, subject_type, subject_ref, key, kind, fields,
                claimed_by, urgent, created_by
            ) VALUES (
                new_id,
                'operator',
                expired.subject_repo,
                COALESCE(expired.subject_type, 'finding'),
                COALESCE(expired.subject_ref, keyed),
                keyed,
                'decision',
                jsonb_build_object(
                    'kind', 'decision',
                    'ticket', CASE WHEN expired.subject_type = 'issue' THEN
                        jsonb_build_object(
                            'repo', expired.subject_repo, 'number', expired.subject_ref::bigint
                        )
                    END,
                    'summary', format(
                        'The %s item %s ran out of time twice in a row (runs %s), so the '
                        'pipeline does not start it again by itself. Only the first option '
                        'starts it again; any other answer, a reply or a reject closes it.',
                        expired.kind, expired.id, array_to_string(uncounted, ' and ')
                    ),
                    'options', jsonb_build_array(
                        'Try again: the item goes back in the queue, for two more runs',
                        'Stop: close the item as superseded'
                    )
                ),
                expire_claims.holder,
                expired.urgent,
                actor
            );
            INSERT INTO public.item_after (item_id, after_id) VALUES (expired.id, new_id);
            UPDATE public.claims AS c
                SET decision_id = new_id
                WHERE c.item_id = expired.id AND c.ending = 'expired' AND c.decision_id IS NULL;
        END IF;

        expired_item := expired.id;
        expired_run := COALESCE(ended_run, split_part(expired.claimed_by, ':', 2));
        decision := new_id;
        RETURN NEXT;
    END LOOP;
END
$$;

REVOKE ALL ON FUNCTION expire_claims(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION expire_claims(text) TO board_process;

-- A two-expiry decision that closes as answered lets its item go, as any item that is
-- waited on does. That is right for the first option only ("try again"). For any other
-- answer the item closes here, the way a reject or a reply closes it through the cascade,
-- and with the cascade's result, so the same reading finds both.
CREATE FUNCTION items_stop_after_expiries() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    UPDATE public.items AS i
        SET closed_at = now(),
            resolution = 'superseded',
            result = jsonb_build_object(
                'cause', 'after',
                'item', NEW.id,
                'resolution', NEW.resolution
            )
        WHERE i.closed_at IS NULL
          AND i.id IN (SELECT c.item_id FROM public.claims AS c WHERE c.decision_id = NEW.id);
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION items_stop_after_expiries() FROM PUBLIC;

CREATE TRIGGER items_expiry_decision_answered
    AFTER UPDATE ON items
    FOR EACH ROW
    WHEN (
        OLD.closed_at IS NULL
        AND NEW.kind = 'decision'
        AND NEW.resolution = 'answered'
        AND (NEW.result ->> 'option') IS DISTINCT FROM '1'
    )
    EXECUTE FUNCTION items_stop_after_expiries();

-- An item that closes under a claim ends the claim, whoever closed it: the run, the
-- operator's reject, or a cascade. The run's token dies with it.
CREATE FUNCTION claims_end_on_close() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    UPDATE public.claims AS c
        SET ended_at = now(), ending = 'closed'
        WHERE c.item_id = NEW.id AND c.ended_at IS NULL;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION claims_end_on_close() FROM PUBLIC;

CREATE TRIGGER items_close_ends_claim
    AFTER UPDATE ON items
    FOR EACH ROW
    WHEN (OLD.closed_at IS NULL AND NEW.closed_at IS NOT NULL)
    EXECUTE FUNCTION claims_end_on_close();

-- Whose token this is: the run, its item and its stage, for a token whose claim is still
-- running. A token is dead once the item closes or the claim ends or runs out, counted or
-- not. The server asks before any caller is known, so this is board_app's own, like the
-- login proxy's table; it tells a caller nothing it could not already prove.
CREATE FUNCTION run_of_token(token_sha256 text)
    RETURNS TABLE (token_run text, token_item text, token_stage text)
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT c.run, c.item_id, i.kind
    FROM public.claims AS c
    JOIN public.items AS i ON i.id = c.item_id
    WHERE c.token_sha256 = run_of_token.token_sha256
      AND c.ended_at IS NULL
      AND c.expires_at > now()
      AND i.closed_at IS NULL
      AND i.claimed_by = 'run:' || c.run
$$;

REVOKE ALL ON FUNCTION run_of_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION run_of_token(text) TO board_app;
