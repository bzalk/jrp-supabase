\set pguser `echo "$POSTGRES_USER"`

CREATE DATABASE _supabase WITH OWNER supabase_admin;

\c _supabase
CREATE SCHEMA IF NOT EXISTS public AUTHORIZATION supabase_admin;
GRANT USAGE, CREATE ON SCHEMA public TO supabase_admin;
GRANT CREATE ON DATABASE _supabase TO supabase_admin;
ALTER DATABASE _supabase SET search_path TO _analytics, public;
ALTER ROLE supabase_admin IN DATABASE _supabase SET search_path TO _analytics, public;

\c postgres
