\set pguser `echo "$POSTGRES_USER"`

\c _supabase
CREATE SCHEMA IF NOT EXISTS _supavisor AUTHORIZATION supabase_admin;
ALTER SCHEMA _supavisor OWNER TO supabase_admin;
GRANT USAGE, CREATE ON SCHEMA _supavisor TO supabase_admin;
\c postgres
