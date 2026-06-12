#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${JRP_INSTALL_DIR:-/opt/jrp-supabase}"
DOCKER_DIR="${INSTALL_DIR}/docker"
REPAIR_LOG_DIR="${JRP_REPAIR_LOG_DIR:-${DOCKER_DIR}/repair-logs}"
REPAIR_RUN_ID="${JRP_REPAIR_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
REPAIR_LOG_FILE="${REPAIR_LOG_DIR}/realtime-tenant-${REPAIR_RUN_ID}.log"
REPAIR_LATEST_LOG="${REPAIR_LOG_DIR}/realtime-tenant-latest.log"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.traefik.yml)
REALTIME_SERVICE="${REALTIME_SERVICE:-realtime}"
REALTIME_CONTAINER="${REALTIME_CONTAINER:-realtime-dev.supabase-realtime}"
REALTIME_TENANT_ID="${REALTIME_TENANT_ID:-realtime-dev}"

log() {
  printf '[jrp-realtime-repair] %s\n' "$*"
}

fail() {
  printf '[jrp-realtime-repair] ERROR: %s\n' "$*" >&2
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

require_stack() {
  [ -d "$DOCKER_DIR" ] || fail "Docker stack directory not found: ${DOCKER_DIR}"
  [ -f "${DOCKER_DIR}/docker-compose.yml" ] || fail "docker-compose.yml not found"
  [ -f "${DOCKER_DIR}/docker-compose.traefik.yml" ] || fail "docker-compose.traefik.yml not found"
  [ -f "${DOCKER_DIR}/.env" ] || fail ".env not found"
  command -v docker >/dev/null 2>&1 || fail "docker is not installed"
  docker compose version >/dev/null
}

ensure_db_healthy() {
  cd "$DOCKER_DIR"
  log "Ensuring Supabase database is running"
  docker compose "${COMPOSE_FILES[@]}" up -d --no-deps db

  local status
  local attempt
  for attempt in $(seq 1 60); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' supabase-db 2>/dev/null || true)"
    if [ "$status" = "healthy" ] || [ "$status" = "running" ]; then
      log "Supabase database status: ${status}"
      return
    fi
    log "Waiting for supabase-db to become healthy (${status:-unknown})"
    sleep 3
  done

  docker logs --tail 120 supabase-db 2>&1 || true
  fail "Timed out waiting for supabase-db to become healthy"
}

read_env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $1 == key {
      sub(/^[^=]*=/, "")
      gsub(/^"|"$/, "")
      gsub(/^'\''|'\''$/, "")
      print
      exit
    }
  ' "${DOCKER_DIR}/.env"
}

db_psql() {
  local password
  password="$(docker exec supabase-db sh -c 'printf %s "$POSTGRES_PASSWORD"' 2>/dev/null || true)"
  [ -n "$password" ] || password="$(read_env_value POSTGRES_PASSWORD)"
  [ -n "$password" ] || fail "POSTGRES_PASSWORD is empty; cannot inspect Realtime metadata"
  docker exec -i -e PGPASSWORD="$password" supabase-db \
    psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -h localhost -U postgres -d postgres "$@"
}

print_realtime_metadata() {
  log "Discovering Realtime metadata tables"
  db_psql -At <<SQL || true
select table_schema || '.' || table_name
from information_schema.tables
where table_schema ilike '%realtime%'
   or table_name ilike '%tenant%'
order by table_schema, table_name;
SQL

  log "Checking tenant rows for ${REALTIME_TENANT_ID}"
  db_psql <<SQL || true
do \$\$
declare
  rel regclass;
  found_count integer;
begin
  foreach rel in array array[
    to_regclass('realtime.tenants'),
    to_regclass('_realtime.tenants')
  ] loop
    if rel is not null then
      execute format('select count(*) from %s where external_id = \$1', rel)
        into found_count
        using '${REALTIME_TENANT_ID}';
      raise notice '% rows for external_id=%: %', rel::text, '${REALTIME_TENANT_ID}', found_count;
    end if;
  end loop;
end
\$\$;
SQL
}

ensure_publication_exists() {
  log "Ensuring supabase_realtime publication exists"
  db_psql <<'SQL'
do $$
begin
  if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    execute 'create publication supabase_realtime';
  end if;
end
$$;
SQL
}

reset_realtime_metadata_schema() {
  log "Resetting _realtime metadata schema"
  db_psql <<'SQL'
drop schema if exists _realtime cascade;
create schema _realtime authorization supabase_admin;
grant usage, create on schema _realtime to supabase_admin;
SQL
}

run_realtime_seed() {
  cd "$DOCKER_DIR"
  log "Stopping Realtime service before metadata reset"
  docker compose "${COMPOSE_FILES[@]}" stop "$REALTIME_SERVICE" || true

  local seed_code
  set +e
  reset_realtime_metadata_schema
  seed_code=$?
  if [ "$seed_code" -eq 0 ]; then
    log "Running Realtime migrations on clean metadata schema"
    docker compose "${COMPOSE_FILES[@]}" run --rm --no-deps \
      --entrypoint /app/bin/migrate "$REALTIME_SERVICE"
    seed_code=$?
  fi
  if [ "$seed_code" -eq 0 ]; then
    log "Running Realtime self-host seed for tenant ${REALTIME_TENANT_ID}"
    docker compose "${COMPOSE_FILES[@]}" run --rm --no-deps \
      --entrypoint /app/bin/realtime "$REALTIME_SERVICE" \
      eval 'Realtime.Release.seeds(Realtime.Repo)'
    seed_code=$?
  fi
  set -e

  log "Starting Realtime service"
  docker compose "${COMPOSE_FILES[@]}" up -d --no-deps "$REALTIME_SERVICE"
  [ "$seed_code" -eq 0 ] || fail "Realtime seed exited with code ${seed_code}"
}

wait_for_realtime_health() {
  cd "$DOCKER_DIR"
  local anon_key
  anon_key="$(read_env_value ANON_KEY)"
  [ -n "$anon_key" ] || fail "ANON_KEY is missing from .env"

  log "Waiting for Realtime tenant health endpoint"
  local attempt
  for attempt in $(seq 1 40); do
    if docker compose "${COMPOSE_FILES[@]}" exec -T "$REALTIME_SERVICE" \
      curl -sSfL --head -o /dev/null \
        -H "Authorization: Bearer ${anon_key}" \
        "http://localhost:4000/api/tenants/${REALTIME_TENANT_ID}/health"; then
      log "Realtime tenant ${REALTIME_TENANT_ID} is healthy"
      return
    fi
    sleep 3
  done

  docker logs --tail 120 "$REALTIME_CONTAINER" 2>&1 || true
  fail "Timed out waiting for Realtime tenant ${REALTIME_TENANT_ID} to become healthy"
}

main() {
  init_logging
  require_stack
  ensure_db_healthy
  log "Repairing Realtime tenant ${REALTIME_TENANT_ID}"
  print_realtime_metadata
  ensure_publication_exists
  run_realtime_seed
  print_realtime_metadata
  wait_for_realtime_health
  log "Realtime tenant repair completed"
}

main "$@"
