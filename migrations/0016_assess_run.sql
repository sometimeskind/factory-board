-- What an assess run needs from the database: it writes its issue's `work` item, and a
-- close as `wait` makes the board write the issue's next `assess` item, dated.

-- ---- An assess run's `work` item ----

-- 0012 kept every run from inserting a stage's item, and said this migration would loosen
-- it. A run may now insert one kind of stage item: a `work` item about an issue, held by
-- nobody, while the run itself holds that issue's open `assess` item. Its claim has not
-- run out when `claimed_by` still names it and `claim_expires` is ahead. Every other
-- stage's item, a `work` item about a PR (the reconciler's continuation item) and a
-- `work` item about another issue stay refused.
--
-- The subquery reads `items` as the caller, who may read every row (`items_select`).
-- The fix-forward limit (0009) is a trigger, so it still counts this insert.
DROP POLICY items_insert_no_run_stage_item ON items;

CREATE POLICY items_insert_no_run_stage_item ON items
    AS RESTRICTIVE
    FOR INSERT TO board_agent
    WITH CHECK (
        assignee = 'operator'
        OR current_setting('board.actor', true) !~ '^run:'
        OR (
            assignee = 'work'
            AND subject_type = 'issue'
            AND claimed_by IS NULL
            AND EXISTS (
                SELECT FROM items AS held
                WHERE held.closed_at IS NULL
                  AND held.kind = 'assess'
                  AND held.claimed_by = current_setting('board.actor', true)
                  AND held.claim_expires > now()
                  AND held.subject_type = 'issue'
                  AND held.subject_repo = items.subject_repo
                  AND held.subject_ref = items.subject_ref
            )
        )
    );

-- ---- The dated `assess` item of a `wait` ----

-- A run can't write its issue's next `assess` item: its own is still open on the same
-- subject, and one item is open per stage and subject. So `wait` is a field of the close
-- (`AssessResult.wait`: `not_before` and `note`), and this writes the item in the close's
-- transaction, as `items_carry_pending_wake` does for a wake that waited.
--
-- It fires after that trigger (the name sorts later), so a wake that waited has written
-- the next item already, due now. That item then takes the reason and the note and keeps
-- its date: the earlier date wins, and no date is the earliest, as in
-- `merge_assess_trigger`. With no open item, a new one is written for the date.
--
-- It reads `result`, which the server validates and the database does not. A date that
-- does not parse writes nothing, and never raises: a close must not fail on it. The
-- reconciler's sweep finds an issue left with no item.
CREATE FUNCTION items_write_dated_assess() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    wake timestamptz;
    note text := NEW.result #>> '{wait,note}';
    new_id text;
BEGIN
    BEGIN
        wake := (NEW.result #>> '{wait,not_before}')::timestamptz;
    EXCEPTION WHEN data_exception THEN
        RETURN NULL;
    END;
    IF wake IS NULL OR NOT isfinite(wake) THEN
        RETURN NULL;
    END IF;
    -- Again when the open item is closed or written by someone else at this very moment.
    FOR attempt IN 1..3 LOOP
        UPDATE public.items AS i
            SET not_before = CASE
                    WHEN i.not_before IS NULL THEN NULL
                    ELSE LEAST(i.not_before, wake)
                END,
                reasons = CASE
                    WHEN 'dated' = ANY (i.reasons) THEN i.reasons
                    ELSE i.reasons || ARRAY['dated']
                END,
                fields = jsonb_set(i.fields, '{note}', COALESCE(to_jsonb(note), 'null'::jsonb))
            WHERE i.closed_at IS NULL
              AND i.assignee = 'assess'
              AND i.subject_type = NEW.subject_type
              AND i.subject_repo = NEW.subject_repo
              AND i.subject_ref = NEW.subject_ref;
        IF FOUND THEN
            RETURN NULL;
        END IF;
        LOOP
            new_id := public.board_new_item_id();
            EXIT WHEN NOT EXISTS (SELECT FROM public.items AS i WHERE i.id = new_id);
        END LOOP;
        INSERT INTO public.items (
            id, assignee, subject_repo, subject_type, subject_ref, kind, fields, reasons,
            not_before, created_by
        ) VALUES (
            new_id,
            'assess',
            NEW.subject_repo,
            NEW.subject_type,
            NEW.subject_ref,
            'assess',
            -- The fields as the `Assess` model writes them, as in `board_wake_assess`.
            jsonb_build_object(
                'kind', 'assess',
                'issue', CASE WHEN NEW.subject_type = 'issue' THEN
                    jsonb_build_object('repo', NEW.subject_repo, 'number', NEW.subject_ref::bigint)
                END,
                'pr', CASE WHEN NEW.subject_type = 'pr' THEN
                    jsonb_build_object('repo', NEW.subject_repo, 'number', NEW.subject_ref::bigint)
                END,
                'note', note
            ),
            ARRAY['dated'],
            wake,
            'process:board'
        )
        ON CONFLICT DO NOTHING;
        IF FOUND THEN
            RETURN NULL;
        END IF;
    END LOOP;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION items_write_dated_assess() FROM PUBLIC;

-- Only a run closes an `assess` item as done with an outcome of this name
-- (`AssessResult`); the reconciler's closes carry a `cause`.
CREATE TRIGGER items_close_writes_dated_assess
    AFTER UPDATE ON items
    FOR EACH ROW
    WHEN (
        OLD.closed_at IS NULL
        AND NEW.closed_at IS NOT NULL
        AND NEW.kind = 'assess'
        AND NEW.subject_type IN ('issue', 'pr')
        AND NEW.resolution = 'done'
        AND NEW.result ->> 'outcome' = 'wait'
    )
    EXECUTE FUNCTION items_write_dated_assess();
