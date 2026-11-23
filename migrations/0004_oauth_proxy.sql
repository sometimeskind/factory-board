-- The login proxy's state: the clients that registered with it (claude.ai) and the tokens
-- it issued, as rows of a key-value table. The server encrypts every value before it
-- writes one, with a key the database never sees.
--
-- This is the one table board_app reads and writes as itself. The proxy runs before any
-- caller is known, so there is no tier to take. The table's shape is the one
-- py-key-value-aio's PostgreSQLStore expects (0.4.6); the server opens that store with
-- auto_create off, because board_app may not create tables.
CREATE TABLE oauth_proxy_kv (
    collection text NOT NULL,
    key text NOT NULL,
    value jsonb NOT NULL,
    ttl double precision,
    created_at timestamptz,
    expires_at timestamptz,
    PRIMARY KEY (collection, key)
);

CREATE INDEX idx_oauth_proxy_kv_expires_at ON oauth_proxy_kv (expires_at)
    WHERE expires_at IS NOT NULL;

-- The store's setup looks the table up in information_schema, which shows a role only the
-- tables it has a right on. The tiers get nothing here.
GRANT USAGE ON SCHEMA public TO board_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_proxy_kv TO board_app;
