-- What the board does by itself: it wakes assess when an issue stops waiting, it refuses a
-- fourth fix-forward `work` item, and it keeps the reconciler's heartbeat.
--
-- Nothing here gives a tier a new right on a row. The triggers run as the owner, and the
-- one function a tier may call (`record_heartbeat`) checks its caller itself.

-- Which issues an item belongs to. Most items belong to one or none:
--   - an item about an issue: that issue;
--   - an operator's item about something else that names a ticket (a `pr-approve` or a
--     `pr-merge` item, a keyed item): the ticket;
--   - a `deploy-check`: the issues its merged PR references, which its writer lists.
-- It reads `fields`, which the server validates and the database does not, so a value that
-- is not a repo and a number is skipped and never raises: a close must not fail on it.
CREATE FUNCTION item_issues(item items) RETURNS TABLE (repo text, issue text)
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT item.subject_repo, item.subject_ref
        WHERE item.subject_type = 'issue'
    UNION
    SELECT item.fields #>> '{ticket,repo}', item.fields #>> '{ticket,number}'
        WHERE item.assignee = 'operator'
          AND item.subject_type IS DISTINCT FROM 'issue'
          AND (item.fields #>> '{ticket,repo}') ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
          AND (item.fields #>> '{ticket,number}') ~ '^[1-9][0-9]*$'
    UNION
    SELECT named.value ->> 'repo', named.value ->> 'number'
        FROM jsonb_array_elements(
            CASE
                WHEN item.kind = 'deploy-check' AND jsonb_typeof(item.fields -> 'issues') = 'array'
                    THEN item.fields -> 'issues'
                ELSE '[]'::jsonb
            END
        ) AS named
        WHERE jsonb_typeof(named.value) = 'object'
          AND (named.value ->> 'repo') ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
          AND (named.value ->> 'number') ~ '^[1-9][0-9]*$'
$$;

-- Only the triggers below call it, as the owner.
REVOKE ALL ON FUNCTION item_issues(items) FROM PUBLIC;

-- ---- The operator acted ----

-- When the operator last acted on an issue: one row per issue, written by the two triggers
-- below and by nothing else. The fix-forward limit counts from it.
CREATE TABLE operator_acts (
    subject_repo text NOT NULL
        CONSTRAINT operator_acts_repo_format
            CHECK (subject_repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
    subject_ref text NOT NULL
        CONSTRAINT operator_acts_issue_number CHECK (subject_ref ~ '^[1-9][0-9]*$'),
    acted_at timestamptz NOT NULL,
    PRIMARY KEY (subject_repo, subject_ref)
);

ALTER TABLE operator_acts ENABLE ROW LEVEL SECURITY;
ALTER TABLE operator_acts FORCE ROW LEVEL SECURITY;

CREATE POLICY operator_acts_owner ON operator_acts
    FOR ALL TO CURRENT_USER
    USING (true) WITH CHECK (true);

-- Every tier reads it, and no tier writes it.
GRANT SELECT ON operator_acts TO board_operator, board_agent, board_process;
CREATE POLICY operator_acts_select ON operator_acts
    FOR SELECT TO board_operator, board_agent, board_process
    USING (true);

-- "The operator acted" is any verb on an item of the issue: done, answer, reject, a reply
-- that closes, defer and bump are all an UPDATE by a `human:` caller. It is also a
-- `pr-approve` item that closes as done: the reconciler closes it on the operator's
-- approval. An item the operator's reject closes down a chain counts for its own issue.
CREATE FUNCTION items_note_operator_act() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    INSERT INTO public.operator_acts (subject_repo, subject_ref, acted_at)
        SELECT named.repo, named.issue, now()
            FROM public.item_issues(NEW) AS named
            ORDER BY 1, 2
        ON CONFLICT (subject_repo, subject_ref) DO UPDATE SET acted_at = EXCLUDED.acted_at;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION items_note_operator_act() FROM PUBLIC;

CREATE TRIGGER items_operator_acted
    AFTER UPDATE ON items
    FOR EACH ROW
    WHEN (
        current_setting('board.actor', true) ~ '^human:'
        OR (OLD.closed_at IS NULL AND NEW.kind = 'pr-approve' AND NEW.resolution = 'done')
    )
    EXECUTE FUNCTION items_note_operator_act();

-- A reply that leaves its item open (the item is someone else's) is a verb too.
CREATE FUNCTION replies_note_operator_act() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    INSERT INTO public.operator_acts (subject_repo, subject_ref, acted_at)
        SELECT named.repo, named.issue, now()
            FROM public.items AS i, LATERAL public.item_issues(i) AS named
            WHERE i.id = NEW.item_id
            ORDER BY 1, 2
        ON CONFLICT (subject_repo, subject_ref) DO UPDATE SET acted_at = EXCLUDED.acted_at;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION replies_note_operator_act() FROM PUBLIC;

CREATE TRIGGER replies_operator_acted
    AFTER INSERT ON replies
    FOR EACH ROW
    WHEN (NEW.author ~ '^human:')
    EXECUTE FUNCTION replies_note_operator_act();

-- ---- The fix-forward limit ----

-- Per issue: the `work` items assess wrote for it since the operator last acted on it, and
-- whether that is the limit of three. Assess's `work` item is about the issue and written
-- by a run; a continuation item is about its PR and written by the reconciler, so it is
-- not counted here (it has its own bound, per PR). A `work` item counts however it ended:
-- a rejected PR counts, and so does an item that closed as stale before any run took it.
--
-- It reads as the caller (security_invoker), who may read both tables anyway.
CREATE VIEW fix_forward WITH (security_invoker = true) AS
    SELECT i.subject_repo,
           i.subject_ref,
           count(*) AS work_items,
           count(*) >= 3 AS at_limit
    FROM items AS i
    LEFT JOIN operator_acts AS a
        ON a.subject_repo = i.subject_repo AND a.subject_ref = i.subject_ref
    WHERE i.kind = 'work'
      AND i.subject_type = 'issue'
      AND i.created_by ~ '^run:'
      AND (a.acted_at IS NULL OR i.created_at > a.acted_at)
    GROUP BY i.subject_repo, i.subject_ref;

GRANT SELECT ON fix_forward TO board_operator, board_agent, board_process;

-- The board refuses a fourth: at three, assess can only write a `decision` item with the
-- evidence. A second write of the `work` item that is still open is not a fourth item: it
-- is left to the uniqueness rule, which answers with the open one.
--
-- Runs as the caller. BRD04: the store reads this code as "the fix-forward limit".
CREATE FUNCTION items_refuse_past_fix_forward_limit() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF EXISTS (
        SELECT FROM public.items AS i
        WHERE i.closed_at IS NULL
          AND i.assignee = 'work'
          AND i.subject_type = 'issue'
          AND i.subject_repo = NEW.subject_repo
          AND i.subject_ref = NEW.subject_ref
    ) THEN
        RETURN NEW;
    END IF;
    IF EXISTS (
        SELECT FROM public.fix_forward AS f
        WHERE f.subject_repo = NEW.subject_repo
          AND f.subject_ref = NEW.subject_ref
          AND f.at_limit
    ) THEN
        RAISE EXCEPTION
            'issue %#% has had three work items since the operator last acted on it: '
            'write a decision item with the evidence instead',
            NEW.subject_repo, NEW.subject_ref
            USING ERRCODE = 'BRD04';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER items_work_within_fix_forward_limit
    BEFORE INSERT ON items
    FOR EACH ROW
    WHEN (NEW.kind = 'work' AND NEW.subject_type = 'issue' AND NEW.created_by ~ '^run:')
    EXECUTE FUNCTION items_refuse_past_fix_forward_limit();

-- ---- Waking assess ----

-- Write the issue's `assess` item, or merge into the one that is open: it is due now (no
-- date is the earliest) and gets the reason if it does not have it. An item a run holds is
-- left alone, as in merge_assess_trigger. The item is ordinary, held by nobody, and written
-- by `process:board`, which is no token's identity.
--
-- Only the trigger below calls it; no tier may.
CREATE FUNCTION board_wake_assess(repo text, issue text, reason text) RETURNS void
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    new_id text;
BEGIN
    -- Twice more if the reconciler writes or closes the issue's item at this very moment.
    FOR attempt IN 1..3 LOOP
        UPDATE public.items AS i
            SET not_before = NULL,
                reasons = CASE
                    WHEN board_wake_assess.reason = ANY (i.reasons) THEN i.reasons
                    ELSE i.reasons || board_wake_assess.reason
                END
            WHERE i.closed_at IS NULL
              AND i.assignee = 'assess'
              AND i.subject_type = 'issue'
              AND i.subject_repo = board_wake_assess.repo
              AND i.subject_ref = board_wake_assess.issue
              AND i.claimed_by IS NULL;
        IF FOUND THEN
            RETURN;
        END IF;
        IF EXISTS (
            SELECT FROM public.items AS i
            WHERE i.closed_at IS NULL
              AND i.assignee = 'assess'
              AND i.subject_type = 'issue'
              AND i.subject_repo = board_wake_assess.repo
              AND i.subject_ref = board_wake_assess.issue
        ) THEN
            RETURN;
        END IF;
        LOOP
            new_id := public.board_new_item_id();
            EXIT WHEN NOT EXISTS (SELECT FROM public.items AS i WHERE i.id = new_id);
        END LOOP;
        INSERT INTO public.items (
            id, assignee, subject_repo, subject_type, subject_ref, kind, fields, reasons,
            created_by
        ) VALUES (
            new_id,
            'assess',
            board_wake_assess.repo,
            'issue',
            board_wake_assess.issue,
            'assess',
            jsonb_build_object(
                'kind', 'assess',
                'issue', jsonb_build_object(
                    'repo', board_wake_assess.repo, 'number', board_wake_assess.issue::bigint
                ),
                'note', NULL
            ),
            ARRAY[board_wake_assess.reason],
            'process:board'
        )
        ON CONFLICT DO NOTHING;
        IF FOUND THEN
            RETURN;
        END IF;
    END LOOP;
END
$$;

REVOKE ALL ON FUNCTION board_wake_assess(text, text, text) FROM PUBLIC;

-- The facts that live in the board, acted on in the transaction that makes them true.
--
-- The operator's last open item on an issue closed (`operator-items-closed`): the item is
-- about the issue and holds it, and no other open item of the operator's holds that issue.
-- A deferred or a waiting item is open. It does not matter how the item closed or who
-- closed it: the issue has stopped waiting on the operator, so it is assess's again.
--   - An item that does not hold its issue wakes nothing.
--   - A `pr-approve` or `pr-merge` item is about its PR and holds no issue. It wakes its
--     ticket's issue only when the operator said no (rejected, or replied), and only when
--     no item of the operator's holds that issue. Approved or merged, the PR goes on by
--     itself; superseded, the reconciler writes the next one.
--   - The decision written after two expired claims wakes nothing when it sends its item
--     back to the queue ("try again"), and nothing when its item is the issue's own
--     `assess` item: the operator stopped that assessment.
--
-- The last open `deploy-check` on an issue closed (`deploy-checks-closed`): for each issue
-- the item names, when no other open `deploy-check` names it. However it closed, and also
-- for one written already closed: that is how a merged PR's issue returns to assess.
--
-- One lock for all of it, so two closes at the same moment can't each see the other's item
-- still open and both wake nothing. It is taken after the cascade's lock, never before.
CREATE FUNCTION items_wake_assess() RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    target record;
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
                    target.repo, target.issue, 'operator-items-closed'
                );
            END IF;
        END LOOP;
    ELSE
        FOR target IN
            SELECT named.repo, named.issue FROM public.item_issues(NEW) AS named ORDER BY 1, 2
        LOOP
            IF NOT EXISTS (
                SELECT FROM public.items AS i, LATERAL public.item_issues(i) AS other
                WHERE i.closed_at IS NULL
                  AND i.kind = 'deploy-check'
                  AND other.repo = target.repo
                  AND other.issue = target.issue
            ) THEN
                PERFORM public.board_wake_assess(
                    target.repo, target.issue, 'deploy-checks-closed'
                );
            END IF;
        END LOOP;
    END IF;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION items_wake_assess() FROM PUBLIC;

CREATE TRIGGER items_close_wakes_assess
    AFTER UPDATE ON items
    FOR EACH ROW
    WHEN (
        OLD.closed_at IS NULL
        AND NEW.closed_at IS NOT NULL
        AND (NEW.assignee = 'operator' OR NEW.kind = 'deploy-check')
    )
    EXECUTE FUNCTION items_wake_assess();

-- ---- The reconciler's heartbeat ----

-- When a process last finished a pass: one row per process. The metrics read it, and an
-- alert on its age says the reconciler has stopped.
CREATE TABLE heartbeats (
    process text PRIMARY KEY
        CONSTRAINT heartbeats_process_format CHECK (process ~ '^[a-z][a-z0-9-]{0,39}$'),
    beat_at timestamptz NOT NULL
);

ALTER TABLE heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE heartbeats FORCE ROW LEVEL SECURITY;

CREATE POLICY heartbeats_owner ON heartbeats
    FOR ALL TO CURRENT_USER
    USING (true) WITH CHECK (true);

GRANT SELECT ON heartbeats TO board_operator, board_agent, board_process;
CREATE POLICY heartbeats_select ON heartbeats
    FOR SELECT TO board_operator, board_agent, board_process
    USING (true);

-- The reconciler only, and only its own row, at the database's own time. Returns the time.
CREATE FUNCTION record_heartbeat() RETURNS timestamptz
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text := current_setting('board.actor', true);
    beat timestamptz;
BEGIN
    IF actor IS DISTINCT FROM 'process:reconciler' THEN
        RAISE EXCEPTION 'only the reconciler records a pass'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO public.heartbeats AS h (process, beat_at)
        VALUES ('reconciler', clock_timestamp())
        ON CONFLICT (process) DO UPDATE SET beat_at = EXCLUDED.beat_at
        RETURNING h.beat_at INTO beat;
    RETURN beat;
END
$$;

REVOKE ALL ON FUNCTION record_heartbeat() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_heartbeat() TO board_process;
