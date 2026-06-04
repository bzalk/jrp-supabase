-- NOTE: change to your own passwords for production environments
\set pgpass `echo "$POSTGRES_PASSWORD"`

SELECT set_config('jrp.pgpass', :'pgpass', false);

DO $$
DECLARE
  pgpass text := current_setting('jrp.pgpass');
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    CREATE ROLE service_role NOLOGIN BYPASSRLS;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_admin') THEN
    EXECUTE format(
      'CREATE ROLE supabase_admin WITH LOGIN SUPERUSER CREATEDB CREATEROLE REPLICATION BYPASSRLS PASSWORD %L',
      pgpass
    );
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticator') THEN
    EXECUTE format('CREATE ROLE authenticator WITH LOGIN NOINHERIT PASSWORD %L', pgpass);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgbouncer') THEN
    EXECUTE format('CREATE ROLE pgbouncer WITH LOGIN NOINHERIT PASSWORD %L', pgpass);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_auth_admin') THEN
    EXECUTE format('CREATE ROLE supabase_auth_admin WITH LOGIN NOINHERIT CREATEROLE PASSWORD %L', pgpass);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_functions_admin') THEN
    EXECUTE format('CREATE ROLE supabase_functions_admin WITH LOGIN NOINHERIT CREATEROLE PASSWORD %L', pgpass);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_storage_admin') THEN
    EXECUTE format('CREATE ROLE supabase_storage_admin WITH LOGIN NOINHERIT CREATEROLE PASSWORD %L', pgpass);
  END IF;

  EXECUTE format('ALTER ROLE postgres WITH PASSWORD %L', pgpass);
  EXECUTE format('ALTER ROLE supabase_admin WITH PASSWORD %L', pgpass);
  EXECUTE format('ALTER ROLE authenticator WITH PASSWORD %L', pgpass);
  EXECUTE format('ALTER ROLE pgbouncer WITH PASSWORD %L', pgpass);
  EXECUTE format('ALTER ROLE supabase_auth_admin WITH PASSWORD %L', pgpass);
  EXECUTE format('ALTER ROLE supabase_functions_admin WITH PASSWORD %L', pgpass);
  EXECUTE format('ALTER ROLE supabase_storage_admin WITH PASSWORD %L', pgpass);
END
$$;

GRANT anon, authenticated, service_role TO authenticator;

GRANT CREATE ON DATABASE postgres TO supabase_auth_admin, supabase_storage_admin, supabase_functions_admin, supabase_admin;
GRANT USAGE, CREATE ON SCHEMA public TO supabase_auth_admin, supabase_storage_admin, supabase_functions_admin, supabase_admin;

CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION supabase_auth_admin;
CREATE SCHEMA IF NOT EXISTS storage AUTHORIZATION supabase_storage_admin;
CREATE SCHEMA IF NOT EXISTS realtime AUTHORIZATION supabase_admin;
CREATE SCHEMA IF NOT EXISTS _realtime AUTHORIZATION supabase_admin;
CREATE SCHEMA IF NOT EXISTS graphql_public AUTHORIZATION supabase_admin;
