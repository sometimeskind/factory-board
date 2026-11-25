-- The seat on a claim, and the two pause changes the stages need before the first one runs.

-- ---- The seat ----

-- A seat is a place at a stage, of a type: `review/1` is a normal seat, `review/urgent-1`
-- an urgent one. How many a stage has is the dispatcher's to know (the factory definition);
-- the board keeps the name on the claim and holds two rules: the seat is at the item's
-- stage, and one run sits in it at a time. A claim made before this migration has none.
ALTER TABLE claims
    ADD COLUMN seat text
        CONSTRAINT claims_seat_format
            CHECK (seat ~ '^[a-z][a-z-]{0,29}/(urgent-)?[1-9][0-9]?$');

-- At most one running claim per seat. A claim that ran out still sits in its seat until
-- the dispatcher counts the expiry, which it does before it reads the queue.
CREATE UNIQUE INDEX claims_seat_occupied ON claims (seat) WHERE ended_at IS NULL;

GRANT SELECT (seat) ON claims TO board_operator, board_agent, board_process;

-- ---- The pause, set by the reconciler too ----

-- The reconciler's tripwire and the self-stop set the pause. Only a person lifts it:
-- `lift_pause` is unchanged.
ALTER TABLE pause
    DROP CONSTRAINT pause_paused_by_format,
    ADD CONSTRAINT pause_paused_by_format
        CHECK (paused_by ~ '^(human:[A-Za-z0-9][A-Za-z0-9._-]{0,99}|process:reconciler)$');

CREATE OR REPLACE FUNCTION set_pause(why text)
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
    IF actor IS NULL
        OR (
            actor !~ '^human:[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
            AND actor <> 'process:reconciler'
        )
    THEN
        RAISE EXCEPTION 'only the operator or the reconciler pauses the factory'
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

GRANT EXECUTE ON FUNCTION set_pause(text) TO board_process;

-- ---- The claim: from a seat, and not while the factory is paused ----

-- As in 0008_claims.sql, with two additions.
--
-- The seat: the claim names it, it is at the item's stage, an urgent seat takes an urgent
-- item only, and nobody sits in it (BRD03, like any item that can't be claimed now).
--
-- The pause: while it stands nothing is claimed but a `health` or a `digest` item (BRD05:
-- the store reads this code as "paused"). Until now only the dispatcher's own gate kept
-- the pause; the board refuses too, so a dispatcher that misreads the flag starts nothing.
-- A stale `work` item is not closed while paused either: the refusal comes first.
DROP FUNCTION claim_item(text, text, integer, text, text);

CREATE FUNCTION claim_item(
    item text, run text, cap_minutes integer, token_sha256 text, current_hash text, seat text
) RETURNS text
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text := current_setting('board.actor', true);
    found_item public.items%ROWTYPE;
    seated text;
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
    IF seat IS NULL OR seat !~ '^[a-z][a-z-]{0,29}/(urgent-)?[1-9][0-9]?$' THEN
        RAISE EXCEPTION 'a seat is <stage>/<n> or <stage>/urgent-<n>, with n from 1 to 99'
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
    IF found_item.kind NOT IN ('health', 'digest') AND EXISTS (SELECT FROM public.pause) THEN
        RAISE EXCEPTION
            'the factory is paused: only a health or a digest item is claimed, not a % item',
            found_item.kind
            USING ERRCODE = 'BRD05';
    END IF;
    IF EXISTS (SELECT FROM public.claims AS c WHERE c.run = claim_item.run) THEN
        RAISE EXCEPTION 'run % has claimed before; a run claims once', run
            USING ERRCODE = 'check_violation';
    END IF;

    IF split_part(seat, '/', 1) <> found_item.kind THEN
        RAISE EXCEPTION 'seat % is not at the % stage', seat, found_item.kind
            USING ERRCODE = 'check_violation';
    END IF;
    IF split_part(seat, '/', 2) ~ '^urgent-' AND NOT found_item.urgent THEN
        RAISE EXCEPTION 'seat % is for an urgent item, and item % is not urgent', seat, item
            USING ERRCODE = 'check_violation';
    END IF;
    -- One claim at a time is made (the dispatcher is one process), and the unique index
    -- stands behind this for the case that two ever race.
    SELECT c.run INTO seated
        FROM public.claims AS c
        WHERE c.seat = claim_item.seat AND c.ended_at IS NULL;
    IF FOUND THEN
        RAISE EXCEPTION 'seat % is taken by run %', seat, seated USING ERRCODE = 'BRD03';
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
    INSERT INTO public.claims (run, item_id, token_sha256, expires_at, seat)
        VALUES (
            claim_item.run,
            claim_item.item,
            claim_item.token_sha256,
            now() + make_interval(mins => claim_item.cap_minutes + 10),
            claim_item.seat
        );
    RETURN 'claimed';
END
$$;

REVOKE ALL ON FUNCTION claim_item(text, text, integer, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_item(text, text, integer, text, text, text) TO board_process;
