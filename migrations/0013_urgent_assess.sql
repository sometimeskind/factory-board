-- An unhealthy deploy check wakes an urgent `assess` item, and one on the PR when the PR
-- references no issue (0009 woke nothing for it).
--
-- A change that is live and unhealthy is the thing the factory should look at first, so
-- the `assess` item that follows it is urgent: it is taken first and may sit in an urgent
-- seat. A healthy or a stuck close wakes as before, not urgent. Nothing here makes an
-- urgent item ordinary again: a later wake that is not urgent leaves `urgent` as it is.

-- ---- Urgency that waits on a held item ----

-- Like `pending_reasons` (0009), and for the same reason: a wake that meets an `assess` item
-- a run holds must not change what the run was started on, and must not be lost. An urgent
-- one also leaves its urgency here, for the item the run's end hands it to: the item itself
-- when its claim is released or runs out, the issue's next `assess` item when it closes.
-- An urgent wake always brings a reason, so there is never urgency without one pending.
-- No tier has a grant on the column: only the functions below write it.
ALTER TABLE items
    ADD COLUMN pending_urgent boolean NOT NULL DEFAULT false
    CONSTRAINT items_pending_urgent_with_reasons
        CHECK (NOT pending_urgent OR cardinality(pending_reasons) > 0);

-- ---- The wake, on an issue or on a PR, urgent or not ----

-- As in 0009, with two additions:
--   - `urgent`: a new item is written urgent, an open one that nobody holds becomes urgent,
--     and a held one keeps the urgency pending. A wake that is not urgent changes no
--     item's urgency;
--   - the subject may be a PR, for a deploy check whose PR references no issue. Its
--     `assess` item names the PR in `pr` and no issue, and is one per PR like any stage's
--     item (`items_open_stage_subject`): a second wake takes the open one.
--
-- The argument list changed, so the old function goes: nothing may call the one that can't
-- be urgent. Only the triggers below call it; no tier may.
DROP FUNCTION board_wake_assess(text, text, text[]);

CREATE FUNCTION board_wake_assess(
    repo text, subject_type text, ref text, reasons text[], urgent boolean
) RETURNS void
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    new_id text;
BEGIN
    IF board_wake_assess.subject_type IS NULL
        OR board_wake_assess.subject_type NOT IN ('issue', 'pr')
    THEN
        RAISE EXCEPTION 'assess is woken for an issue or a PR, not for %',
            COALESCE(board_wake_assess.subject_type, 'nothing')
            USING ERRCODE = 'check_violation';
    END IF;
    -- A held item's urgency waits only beside a reason (items_pending_urgent_with_reasons).
    IF board_wake_assess.reasons IS NULL
        OR cardinality(board_wake_assess.reasons) = 0
        OR board_wake_assess.urgent IS NULL
    THEN
        RAISE EXCEPTION 'a wake has a reason, and is urgent or not'
            USING ERRCODE = 'check_violation';
    END IF;
    -- Again when the item is closed, released or written by someone else at this very
    -- moment: each statement waits for the other transaction and then finds no row.
    FOR attempt IN 1..3 LOOP
        UPDATE public.items AS i
            SET not_before = NULL,
                urgent = i.urgent OR board_wake_assess.urgent,
                reasons = i.reasons || ARRAY(
                    SELECT t.name
                    FROM unnest(board_wake_assess.reasons) WITH ORDINALITY AS t (name, position)
                    WHERE t.name <> ALL (i.reasons)
                    ORDER BY t.position
                )
            WHERE i.closed_at IS NULL
              AND i.assignee = 'assess'
              AND i.subject_type = board_wake_assess.subject_type
              AND i.subject_repo = board_wake_assess.repo
              AND i.subject_ref = board_wake_assess.ref
              AND i.claimed_by IS NULL;
        IF FOUND THEN
            RETURN;
        END IF;
        UPDATE public.items AS i
            SET pending_reasons = i.pending_reasons || ARRAY(
                    SELECT t.name
                    FROM unnest(board_wake_assess.reasons) WITH ORDINALITY AS t (name, position)
                    WHERE t.name <> ALL (i.pending_reasons)
                    ORDER BY t.position
                ),
                pending_urgent = i.pending_urgent OR board_wake_assess.urgent
            WHERE i.closed_at IS NULL
              AND i.assignee = 'assess'
              AND i.subject_type = board_wake_assess.subject_type
              AND i.subject_repo = board_wake_assess.repo
              AND i.subject_ref = board_wake_assess.ref
              AND i.claimed_by IS NOT NULL;
        IF FOUND THEN
            RETURN;
        END IF;
        LOOP
            new_id := public.board_new_item_id();
            EXIT WHEN NOT EXISTS (SELECT FROM public.items AS i WHERE i.id = new_id);
        END LOOP;
        INSERT INTO public.items (
            id, assignee, subject_repo, subject_type, subject_ref, kind, fields, reasons,
            urgent, created_by
        ) VALUES (
            new_id,
            'assess',
            board_wake_assess.repo,
            board_wake_assess.subject_type,
            board_wake_assess.ref,
            'assess',
            -- The fields as the `Assess` model writes them: the subject in `issue` or in
            -- `pr`, and the other null.
            jsonb_build_object(
                'kind', 'assess',
                'issue', CASE WHEN board_wake_assess.subject_type = 'issue' THEN
                    jsonb_build_object(
                        'repo', board_wake_assess.repo, 'number', board_wake_assess.ref::bigint
                    )
                END,
                'pr', CASE WHEN board_wake_assess.subject_type = 'pr' THEN
                    jsonb_build_object(
                        'repo', board_wake_assess.repo, 'number', board_wake_assess.ref::bigint
                    )
                END,
                'note', NULL
            ),
            board_wake_assess.reasons,
            board_wake_assess.urgent,
            'process:board'
        )
        ON CONFLICT DO NOTHING;
        IF FOUND THEN
            RETURN;
        END IF;
    END LOOP;
END
$$;

REVOKE ALL ON FUNCTION board_wake_assess(text, text, text, text[], boolean) FROM PUBLIC;

-- ---- What a held item's end does with what waited ----

-- Back in the queue: the pending reasons move into `reasons`, as in 0009, and a pending
-- urgency makes the item itself urgent, so the next run is taken first.
CREATE OR REPLACE FUNCTION items_take_pending_reasons() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    NEW.reasons := NEW.reasons || ARRAY(
        SELECT t.name
        FROM unnest(NEW.pending_reasons) WITH ORDINALITY AS t (name, position)
        WHERE t.name <> ALL (NEW.reasons)
        ORDER BY t.position
    );
    NEW.urgent := NEW.urgent OR NEW.pending_urgent;
    NEW.pending_reasons := '{}';
    NEW.pending_urgent := false;
    RETURN NEW;
END
$$;

-- Closed: the next `assess` item is written with what waited, urgent when an urgent wake
-- waited. The held item's own urgency stays with it: its run has dealt with what made it
-- urgent. The subject is the held item's, an issue or a PR.
CREATE OR REPLACE FUNCTION items_carry_pending_wake() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    PERFORM public.board_wake_assess(
        NEW.subject_repo, NEW.subject_type, NEW.subject_ref, NEW.pending_reasons,
        NEW.pending_urgent
    );
    -- The owner may change a closed item: the reasons have moved on.
    UPDATE public.items AS i
        SET pending_reasons = '{}', pending_urgent = false
        WHERE i.id = NEW.id;
    RETURN NULL;
END
$$;

-- An `assess` item on a PR carries its wakes too.
DROP TRIGGER items_close_carries_pending_wake ON items;

CREATE TRIGGER items_close_carries_pending_wake
    AFTER UPDATE ON items
    FOR EACH ROW
    WHEN (
        OLD.closed_at IS NULL
        AND NEW.closed_at IS NOT NULL
        AND NEW.kind = 'assess'
        AND NEW.subject_type IN ('issue', 'pr')
        AND cardinality(NEW.pending_reasons) > 0
    )
    EXECUTE FUNCTION items_carry_pending_wake();

-- ---- The facts that wake assess ----

-- As in 0009 for the operator's items, which never wake urgent. For a deploy check, three
-- changes:
--   - a run's close as `unhealthy` (its result's `outcome`) makes the wake urgent;
--   - an issue is woken once, when the last open deploy check that names it closes, so
--     that close may be a healthy one while another check of the same round found the
--     change unhealthy. The wake is urgent then too: it is urgent when any deploy check
--     that names the issue closed unhealthy after the issue's last `assess` run started
--     (the claim's time), since no run has looked at it yet;
--   - an unhealthy check that names no issue wakes assess on its PR, urgent: assess then
--     files the issue. A healthy or stuck one wakes nothing, as before.
CREATE OR REPLACE FUNCTION items_wake_assess() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    target record;
    unhealthy boolean;
    names_an_issue boolean := false;
BEGIN
    PERFORM pg_advisory_xact_lock(5, 2);
    IF NEW.assignee = 'operator' THEN
        IF EXISTS (
            SELECT FROM public.claims AS c
            JOIN public.items AS stuck ON stuck.id = c.item_id
            WHERE c.decision_id = NEW.id
              AND (
                  (NEW.resolution = 'answered' AND NEW.result ->> 'option' = '1')
                  OR stuck.kind = 'assess'
              )
        ) THEN
            RETURN NULL;
        END IF;
        IF NEW.subject_type = 'issue' THEN
            IF NOT NEW.holds THEN
                RETURN NULL;
            END IF;
        ELSIF NEW.kind NOT IN ('pr-approve', 'pr-merge')
            OR NEW.resolution NOT IN ('rejected', 'replied')
        THEN
            RETURN NULL;
        END IF;
        FOR target IN
            SELECT named.repo, named.issue FROM public.item_issues(NEW) AS named ORDER BY 1, 2
        LOOP
            IF NOT EXISTS (
                SELECT FROM public.items AS i
                WHERE i.closed_at IS NULL
                  AND i.assignee = 'operator'
                  AND i.subject_type = 'issue'
                  AND i.subject_repo = target.repo
                  AND i.subject_ref = target.issue
                  AND i.holds
            ) THEN
                PERFORM public.board_wake_assess(
                    target.repo, 'issue', target.issue, ARRAY['operator-items-closed'], false
                );
            END IF;
        END LOOP;
    ELSE
        -- Only a run closes as done with an outcome of this name (`DeployCheckResult`); the
        -- reconciler's closes carry a `cause`, or `nothing-to-deploy` and its like.
        unhealthy := COALESCE(
            NEW.resolution = 'done' AND NEW.result ->> 'outcome' = 'unhealthy', false
        );
        FOR target IN
            SELECT named.repo, named.issue FROM public.item_issues(NEW) AS named ORDER BY 1, 2
        LOOP
            names_an_issue := true;
            IF NOT EXISTS (
                SELECT FROM public.items AS i, LATERAL public.item_issues(i) AS other
                WHERE i.closed_at IS NULL
                  AND i.kind = 'deploy-check'
                  AND other.repo = target.repo
                  AND other.issue = target.issue
            ) THEN
                PERFORM public.board_wake_assess(
                    target.repo,
                    'issue',
                    target.issue,
                    ARRAY['deploy-checks-closed'],
                    unhealthy OR EXISTS (
                        SELECT FROM public.items AS d, LATERAL public.item_issues(d) AS other
                        WHERE d.kind = 'deploy-check'
                          AND d.resolution = 'done'
                          AND d.result ->> 'outcome' = 'unhealthy'
                          AND other.repo = target.repo
                          AND other.issue = target.issue
                          AND d.closed_at > COALESCE(
                              (
                                  SELECT max(c.claimed_at)
                                  FROM public.claims AS c
                                  JOIN public.items AS a ON a.id = c.item_id
                                  WHERE a.kind = 'assess'
                                    AND a.subject_type = 'issue'
                                    AND a.subject_repo = target.repo
                                    AND a.subject_ref = target.issue
                              ),
                              '-infinity'
                          )
                    )
                );
            END IF;
        END LOOP;
        IF NOT names_an_issue AND unhealthy AND NEW.subject_type = 'pr' THEN
            PERFORM public.board_wake_assess(
                NEW.subject_repo, 'pr', NEW.subject_ref, ARRAY['deploy-checks-closed'], true
            );
        END IF;
    END IF;
    RETURN NULL;
END
$$;
