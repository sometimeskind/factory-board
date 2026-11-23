-- The items table: one row is one thing someone has to do.

-- The roles are made outside the migrations (dev/roles.sql for a throwaway database; the
-- cluster's own configuration otherwise). Stop here, by name, if one is missing.
DO $$
DECLARE
    wanted text;
BEGIN
    FOREACH wanted IN ARRAY ARRAY['board_app', 'board_operator', 'board_agent', 'board_process']
    LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = wanted) THEN
            RAISE EXCEPTION 'role % does not exist', wanted
                USING HINT = 'The migrations do not create roles. Create board_app, '
                    'board_operator, board_agent and board_process first (dev/roles.sql).';
        END IF;
    END LOOP;
END
$$;

CREATE TABLE items (
    -- Eight random characters, made by the store. Never a sequence: after a restore a
    -- sequence would hand out numbers that were already used.
    id text PRIMARY KEY
        CONSTRAINT items_id_format CHECK (id ~ '^[a-hjkmnp-z2-9]{8}$'),

    -- Who the item is for: the operator, a stage, or the merge script.
    assignee text NOT NULL
        CONSTRAINT items_assignee_allowed CHECK (assignee IN (
            'operator',
            'assess', 'work', 'review', 'deploy-check', 'retro', 'health', 'digest', 'merge'
        )),

    -- What the item is about. All null for an item that only has a key.
    subject_repo text
        CONSTRAINT items_subject_repo_format
            CHECK (subject_repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
    subject_type text
        CONSTRAINT items_subject_type_allowed
            CHECK (subject_type IN ('issue', 'pr', 'alert', 'finding')),
    subject_ref text
        CONSTRAINT items_subject_ref_not_empty CHECK (subject_ref <> ''),

    kind text NOT NULL
        CONSTRAINT items_kind_allowed CHECK (kind IN (
            'pr-approve', 'pr-merge', 'terraform-apply', 'kubectl', 'music-tool',
            'decision', 'session', 'manual',
            'assess', 'work', 'review', 'deploy-check', 'retro', 'health', 'digest', 'merge'
        )),
    -- The kind's typed fields. The server validates them with one model per kind.
    fields jsonb NOT NULL DEFAULT '{}'
        CONSTRAINT items_fields_object CHECK (jsonb_typeof(fields) = 'object'),
    -- Where the item's command runs.
    machine text
        CONSTRAINT items_machine_allowed
            CHECK (machine IN ('browser', 'phone-vm', 'workstation', 'pilot')),

    -- The claim holder: the one identity that may close the item.
    claimed_by text
        CONSTRAINT items_claimed_by_identity
            CHECK (claimed_by ~ '^(human|process|run|session):[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    claim_expires timestamptz,

    -- The item is in no queue and on no list before this time.
    not_before timestamptz,
    urgent boolean NOT NULL DEFAULT false,
    -- For an item with no issue (a finding, an alert): at most one open item per key.
    key text
        CONSTRAINT items_key_not_empty CHECK (key <> ''),

    -- What the closer wrote.
    result jsonb
        CONSTRAINT items_result_object CHECK (jsonb_typeof(result) = 'object'),
    closed_at timestamptz,
    resolution text
        CONSTRAINT items_resolution_allowed
            CHECK (resolution IN ('done', 'answered', 'replied', 'rejected', 'superseded')),

    -- For the stages' claims, which a later step builds: expired and released claims are
    -- counted, an assess item collects its triggers, and assess and work items carry the
    -- staleness hash.
    expiries integer NOT NULL DEFAULT 0
        CONSTRAINT items_expiries_not_negative CHECK (expiries >= 0),
    releases integer NOT NULL DEFAULT 0
        CONSTRAINT items_releases_not_negative CHECK (releases >= 0),
    reasons text[] NOT NULL DEFAULT '{}',
    input_hash text,

    -- Provenance: who wrote the item, in which run or session, and whether the operator
    -- typed into that run.
    created_by text NOT NULL
        CONSTRAINT items_created_by_identity
            CHECK (created_by ~ '^(human|process|run|session):[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    created_run text,
    operator_typed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),

    -- An item is open or closed and nothing else.
    CONSTRAINT items_closed_with_resolution CHECK ((closed_at IS NULL) = (resolution IS NULL)),
    CONSTRAINT items_claim_expires_needs_holder
        CHECK (claim_expires IS NULL OR claimed_by IS NOT NULL),
    -- A subject has a type and a reference, and a repo unless it is an alert or a finding.
    CONSTRAINT items_subject_complete CHECK (
        (subject_type IS NULL AND subject_ref IS NULL AND subject_repo IS NULL)
        OR (
            subject_type IS NOT NULL
            AND subject_ref IS NOT NULL
            AND (subject_repo IS NOT NULL OR subject_type IN ('alert', 'finding'))
        )
    ),
    CONSTRAINT items_subject_number
        CHECK (subject_type NOT IN ('issue', 'pr') OR subject_ref ~ '^[1-9][0-9]*$'),
    CONSTRAINT items_subject_or_key CHECK (subject_type IS NOT NULL OR key IS NOT NULL),
    -- The operator's kinds go to the operator; a stage's item has the stage as its kind.
    CONSTRAINT items_kind_fits_assignee CHECK (
        CASE
            WHEN assignee = 'operator' THEN kind IN (
                'pr-approve', 'pr-merge', 'terraform-apply', 'kubectl', 'music-tool',
                'decision', 'session', 'manual'
            )
            ELSE kind = assignee
        END
    ),
    CONSTRAINT items_pr_kind_on_pr
        CHECK (kind NOT IN ('pr-approve', 'pr-merge') OR subject_type = 'pr')
);

-- At most one open item per stage and subject. The operator may have several open items
-- on one issue, and an item with only a key is covered by items_open_key.
CREATE UNIQUE INDEX items_open_stage_subject
    ON items (assignee, subject_repo, subject_type, subject_ref) NULLS NOT DISTINCT
    WHERE closed_at IS NULL AND assignee <> 'operator' AND subject_type IS NOT NULL;

-- At most one open pr-approve or pr-merge item per PR.
CREATE UNIQUE INDEX items_open_pr_item
    ON items (subject_repo, subject_ref)
    WHERE closed_at IS NULL AND kind IN ('pr-approve', 'pr-merge');

-- At most one open item per key.
CREATE UNIQUE INDEX items_open_key
    ON items (key)
    WHERE closed_at IS NULL AND key IS NOT NULL;

-- What a list reads: the open items of one assignee, by date.
CREATE INDEX items_open_by_assignee
    ON items (assignee, not_before)
    WHERE closed_at IS NULL;

-- There is no undo: a closed item stays as it is. Only the table's owner may change one.
CREATE FUNCTION items_refuse_change_when_closed() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF current_user = (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID) THEN
        RETURN NEW;
    END IF;
    -- BRD01: the store reads this code as "the item is closed".
    RAISE EXCEPTION 'item % is closed and cannot be changed', OLD.id USING ERRCODE = 'BRD01';
END
$$;

CREATE TRIGGER items_closed_is_final
    BEFORE UPDATE ON items
    FOR EACH ROW
    WHEN (OLD.closed_at IS NOT NULL)
    EXECUTE FUNCTION items_refuse_change_when_closed();
