-- The board's five roles, for tests, CI and local runs. Run it as a superuser, connected to
-- the board's database. It is safe to run again.
--
-- The migrations never create roles: in a cluster the roles are declared in the cluster's
-- own configuration, with real passwords, and the database's owner cannot create any.
-- The passwords here are for throwaway databases only.
--
--   board_owner     owns the database and the tables; the migrations run as it
--   board_app       the server's login; holds no rights of its own (NOINHERIT) and
--                   takes one tier per transaction with SET LOCAL ROLE
--   board_operator  tier: the operator
--   board_agent     tier: interactive sessions, and later the pipeline's runs
--   board_process   tier: the deterministic scripts

DO $$
DECLARE
    wanted text;
BEGIN
    FOREACH wanted IN ARRAY ARRAY[
        'board_owner', 'board_app', 'board_operator', 'board_agent', 'board_process'
    ] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = wanted) THEN
            EXECUTE format('CREATE ROLE %I', wanted);
        END IF;
    END LOOP;
END
$$;

ALTER ROLE board_owner LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS PASSWORD 'board_owner';
ALTER ROLE board_app LOGIN NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS PASSWORD 'board_app';
ALTER ROLE board_operator NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;
ALTER ROLE board_agent NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;
ALTER ROLE board_process NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;

-- board_app may switch to a tier but inherits nothing from it.
GRANT board_operator, board_agent, board_process TO board_app WITH INHERIT FALSE, SET TRUE;

-- As in the cluster, the database belongs to board_owner, which lets it create tables
-- in the public schema.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I OWNER TO board_owner', current_database());
END
$$;
