-- The operator's replies. An item can carry several, so they have a table of their own.

CREATE TABLE replies (
    -- Random, like an item's ID, so a restore can't reuse one.
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id text NOT NULL REFERENCES items (id),
    author text NOT NULL
        CONSTRAINT replies_author_identity
            CHECK (author ~ '^(human|process|run|session):[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    body text NOT NULL
        CONSTRAINT replies_body_not_empty CHECK (body <> '')
);

CREATE INDEX replies_by_item ON replies (item_id, created_at);
