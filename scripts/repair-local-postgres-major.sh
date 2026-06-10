#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${JRP_INSTALL_DIR:-/opt/jrp-supabase}"
POSTGRES_MAJOR="${POSTGRES_MAJOR:-${1:-}}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-}"
CONFIRM_VALUE="${CONFIRM:-${CONFIRM_RESET:-${2:-}}}"
REPAIR_LOG_DIR="${JRP_REPAIR_LOG_DIR:-${INSTALL_DIR}/docker/repair-logs}"
REPAIR_RUN_ID="${JRP_REPAIR_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
REPAIR_LOG_FILE="${REPAIR_LOG_DIR}/local-postgres-major-${REPAIR_RUN_ID}.log"
REPAIR_LATEST_LOG="${REPAIR_LOG_DIR}/local-postgres-major-latest.log"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.traefik.yml)

log() {
  printf '[jrp-local-postgres-repair] %s\n' "$*"
}

fail() {
  printf '[jrp-local-postgres-repair] ERROR: %s\n' "$*" >&2
  exit 1
}

init_logging() {
  mkdir -p "$REPAIR_LOG_DIR"
  touch "$REPAIR_LOG_FILE"
  chmod 0644 "$REPAIR_LOG_FILE" || true
  ln -sfn "$(basename "$REPAIR_LOG_FILE")" "$REPAIR_LATEST_LOG" 2>/dev/null ||
    cp "$REPAIR_LOG_FILE" "$REPAIR_LATEST_LOG"
  exec > >(tee -a "$REPAIR_LOG_FILE") 2>&1
  log "Full repair log: ${REPAIR_LOG_FILE}"
  log "Latest repair log: ${REPAIR_LATEST_LOG}"
}

validate_inputs() {
  [[ "$POSTGRES_MAJOR" =~ ^[0-9]{2}$ ]] ||
    fail "POSTGRES_MAJOR must be a two-digit major version, for example 17"
  [ "$CONFIRM_VALUE" = "RESET_LOCAL_DATABASE" ] ||
    fail "This is destructive. Re-run with CONFIRM=RESET_LOCAL_DATABASE"
}

default_image_for_major() {
  case "$1" in
    15) printf 'supabase/postgres:15.8.1.085' ;;
    17) printf 'supabase/postgres:17.6.1.084' ;;
    *)
      return 1
      ;;
  esac
}

resolve_postgres_image() {
  if [ -n "$POSTGRES_IMAGE" ]; then
    return
  fi
  POSTGRES_IMAGE="$(default_image_for_major "$POSTGRES_MAJOR")" ||
    fail "No default Supabase Postgres image is configured for major ${POSTGRES_MAJOR}. Set POSTGRES_IMAGE explicitly."
}

require_stack() {
  [ -d "$INSTALL_DIR/docker" ] || fail "docker stack directory not found: ${INSTALL_DIR}/docker"
  [ -f "$INSTALL_DIR/docker/docker-compose.yml" ] || fail "docker-compose.yml not found"
  [ -f "$INSTALL_DIR/docker/docker-compose.traefik.yml" ] || fail "docker-compose.traefik.yml not found"
  [ -f "$INSTALL_DIR/docker/.env" ] || fail ".env not found"
  command -v docker >/dev/null 2>&1 || fail "docker is not installed"
  docker compose version >/dev/null
}

read_env_value() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '
    $1 == key {
      sub(/^[^=]*=/, "")
      gsub(/^"|"$/, "")
      gsub(/^'\''|'\''$/, "")
      print
      exit
    }
  ' "$file"
}

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$file"
  fi
}

ensure_compose_uses_env_image() {
  cd "$INSTALL_DIR/docker"
  if grep -q 'image: ${SUPABASE_POSTGRES_IMAGE:-' docker-compose.yml; then
    return
  fi
  log "Patching docker-compose.yml to use SUPABASE_POSTGRES_IMAGE"
  perl -0pi -e 's|image:\s*supabase/postgres:[^\n]+|image: \${SUPABASE_POSTGRES_IMAGE:-supabase/postgres:15.8.1.085}|' docker-compose.yml
  grep -q 'image: ${SUPABASE_POSTGRES_IMAGE:-' docker-compose.yml ||
    fail "Could not patch db image in docker-compose.yml"
}

current_db_major() {
  cd "$INSTALL_DIR/docker"
  local password version
  password="$(read_env_value .env POSTGRES_PASSWORD)"
  [ -n "$password" ] || return 0
  docker exec -e PGPASSWORD="$password" supabase-db \
    psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -h localhost -U postgres -d postgres -At \
      -c "select current_setting('server_version_num')" 2>/dev/null |
    awk '{ print int($1 / 10000) }'
}

backup_and_reset_db_data() {
  cd "$INSTALL_DIR/docker"
  local backup_root backup_dir
  backup_root="volumes/db/backups"
  backup_dir="${backup_root}/data-before-pg${POSTGRES_MAJOR}-${REPAIR_RUN_ID}"
  mkdir -p "$backup_root"
  if [ -d volumes/db/data ] && [ "$(find volumes/db/data -mindepth 1 -maxdepth 1 2>/dev/null | head -n 1)" ]; then
    log "Moving existing database data directory to ${backup_dir}"
    mv volumes/db/data "$backup_dir"
  else
    rm -rf volumes/db/data
  fi
  mkdir -p volumes/db/data
}

clear_branch_snapshots() {
  cd "$INSTALL_DIR/docker"
  if [ -d branches ]; then
    local backup_dir
    backup_dir="repair-logs/branches-before-pg${POSTGRES_MAJOR}-${REPAIR_RUN_ID}"
    log "Moving existing branch snapshots to ${backup_dir}"
    mkdir -p "$backup_dir"
    find branches -mindepth 1 -maxdepth 1 -exec mv {} "$backup_dir"/ \; 2>/dev/null || true
  fi
  mkdir -p branches
}

ensure_db_config_volume() {
  cd "$INSTALL_DIR/docker"
  log "Ensuring db-config volume has required PostgreSQL custom config directories"
  docker compose "${COMPOSE_FILES[@]}" run --rm --no-deps --entrypoint sh db -lc '
    set -e
    mkdir -p /etc/postgresql-custom/conf.d
    chmod 755 /etc/postgresql-custom /etc/postgresql-custom/conf.d
  '
}

wait_for_db_healthy() {
  local deadline health
  deadline=$((SECONDS + 240))
  while [ "$SECONDS" -lt "$deadline" ]; do
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' supabase-db 2>/dev/null || true)"
    if [ "$health" = "healthy" ]; then
      return
    fi
    log "Waiting for supabase-db to become healthy (${health:-not-created})"
    sleep 3
  done
  docker compose "${COMPOSE_FILES[@]}" ps -a || true
  docker logs --tail 240 supabase-db || true
  fail "Timed out waiting for supabase-db to become healthy"
}

repair_db_roles() {
  cd "$INSTALL_DIR/docker"
  local password
  password="$(read_env_value .env POSTGRES_PASSWORD)"
  [ -n "$password" ] || fail "POSTGRES_PASSWORD is empty; cannot repair database roles"

  log "Ensuring Supabase internal database roles use the configured Postgres password"
  docker exec -i -e PGPASSWORD="$password" supabase-db \
    psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -h localhost -U postgres -d postgres -v pgpass="$password" <<'SQL'
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
    EXECUTE format('CREATE ROLE supabase_admin WITH LOGIN SUPERUSER CREATEDB CREATEROLE REPLICATION BYPASSRLS PASSWORD %L', pgpass);
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
SQL

  if ! docker exec -e PGPASSWORD="$password" supabase-db \
    psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -h localhost -U postgres -d postgres -At \
      -c "SELECT 1 FROM pg_database WHERE datname = '_supabase'" | grep -qx '1'; then
    docker exec -e PGPASSWORD="$password" supabase-db \
      psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -h localhost -U postgres -d postgres \
        -c 'CREATE DATABASE _supabase WITH OWNER supabase_admin'
  fi

  docker exec -i -e PGPASSWORD="$password" supabase-db \
    psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -h localhost -U postgres -d _supabase <<'SQL'
CREATE SCHEMA IF NOT EXISTS public AUTHORIZATION supabase_admin;
CREATE SCHEMA IF NOT EXISTS _analytics AUTHORIZATION supabase_admin;
CREATE SCHEMA IF NOT EXISTS _supavisor AUTHORIZATION supabase_admin;
ALTER SCHEMA public OWNER TO supabase_admin;
ALTER SCHEMA _analytics OWNER TO supabase_admin;
ALTER SCHEMA _supavisor OWNER TO supabase_admin;
GRANT USAGE, CREATE ON SCHEMA public TO supabase_admin;
GRANT USAGE, CREATE ON SCHEMA _analytics TO supabase_admin;
GRANT USAGE, CREATE ON SCHEMA _supavisor TO supabase_admin;
GRANT CREATE ON DATABASE _supabase TO supabase_admin;
ALTER DATABASE _supabase SET search_path TO _analytics, public;
ALTER ROLE supabase_admin IN DATABASE _supabase SET search_path TO _analytics, public;
SQL
}

restart_stack() {
  cd "$INSTALL_DIR/docker"
  log "Stopping database-dependent services"
  docker compose "${COMPOSE_FILES[@]}" stop \
    functions kong studio storage realtime rest auth meta analytics supavisor sync-api db || true

  backup_and_reset_db_data
  clear_branch_snapshots

  log "Pulling ${POSTGRES_IMAGE}"
  docker pull "$POSTGRES_IMAGE"
  ensure_db_config_volume

  log "Starting supabase-db with ${POSTGRES_IMAGE}"
  docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate db
  wait_for_db_healthy
  repair_db_roles

  log "Starting dependent services"
  docker compose "${COMPOSE_FILES[@]}" up -d --build --force-recreate \
    auth rest storage realtime meta analytics supavisor studio kong functions sync-api
  wait_for_db_healthy
}

verify_target_major() {
  local actual
  actual="$(current_db_major || true)"
  [ "$actual" = "$POSTGRES_MAJOR" ] ||
    fail "supabase-db PostgreSQL major is ${actual:-unknown}, expected ${POSTGRES_MAJOR}"
  log "Verified supabase-db PostgreSQL major ${POSTGRES_MAJOR}"
}

main() {
  init_logging
  validate_inputs
  resolve_postgres_image
  require_stack
  ensure_compose_uses_env_image

  cd "$INSTALL_DIR/docker"
  log "Requested local PostgreSQL major: ${POSTGRES_MAJOR}"
  log "Requested local PostgreSQL image: ${POSTGRES_IMAGE}"
  local current
  current="$(current_db_major || true)"
  log "Current supabase-db PostgreSQL major: ${current:-unknown}"
  if [ "$current" = "$POSTGRES_MAJOR" ]; then
    log "supabase-db already runs PostgreSQL major ${POSTGRES_MAJOR}; no database reset needed"
    exit 0
  fi

  cp .env ".env.before-local-postgres-major-${REPAIR_RUN_ID}"
  set_env_value .env SUPABASE_POSTGRES_IMAGE "$POSTGRES_IMAGE"
  restart_stack
  verify_target_major
  log "Local PostgreSQL major repair completed"
}

main "$@"
