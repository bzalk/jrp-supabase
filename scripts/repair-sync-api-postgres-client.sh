#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${JRP_INSTALL_DIR:-/opt/jrp-supabase}"
REPO_BRANCH="${JRP_REPO_BRANCH:-main}"
UPDATE_REPO="${UPDATE_REPO:-false}"
POSTGRES_CLIENT_MAJOR="${POSTGRES_CLIENT_MAJOR:-${1:-17}}"
REPAIR_LOG_DIR="${JRP_REPAIR_LOG_DIR:-${INSTALL_DIR}/docker/repair-logs}"
REPAIR_RUN_ID="${JRP_REPAIR_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
REPAIR_LOG_FILE="${REPAIR_LOG_DIR}/sync-api-postgres-client-${REPAIR_RUN_ID}.log"
REPAIR_LATEST_LOG="${REPAIR_LOG_DIR}/sync-api-postgres-client-latest.log"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.traefik.yml)

log() {
  printf '[jrp-sync-api-client-repair] %s\n' "$*"
}

fail() {
  printf '[jrp-sync-api-client-repair] ERROR: %s\n' "$*" >&2
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

validate_client_major() {
  [[ "$POSTGRES_CLIENT_MAJOR" =~ ^[0-9]{2}$ ]] ||
    fail "POSTGRES_CLIENT_MAJOR must be a two-digit major version, for example 17"
}

require_stack() {
  [ -d "$INSTALL_DIR/docker" ] || fail "docker stack directory not found: ${INSTALL_DIR}/docker"
  [ -f "$INSTALL_DIR/docker/docker-compose.yml" ] || fail "docker-compose.yml not found"
  [ -f "$INSTALL_DIR/docker/docker-compose.traefik.yml" ] || fail "docker-compose.traefik.yml not found"
  [ -f "$INSTALL_DIR/docker/sync-api/Dockerfile" ] || fail "sync-api Dockerfile not found"
  command -v docker >/dev/null 2>&1 || fail "docker is not installed"
  docker compose version >/dev/null
}

update_repo_if_requested() {
  if [ "$UPDATE_REPO" != "true" ]; then
    log "Skipping repo update because UPDATE_REPO=false"
    return
  fi
  command -v git >/dev/null 2>&1 || fail "git is required when UPDATE_REPO=true"
  [ -d "$INSTALL_DIR/.git" ] || fail "install directory is not a git checkout: ${INSTALL_DIR}"
  log "Updating read-only repo checkout from origin/${REPO_BRANCH}"
  git -C "$INSTALL_DIR" fetch origin "$REPO_BRANCH"
  git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$REPO_BRANCH"
}

build_and_restart_sync_api() {
  cd "$INSTALL_DIR/docker"
  log "Building sync-api with PostgreSQL client major ${POSTGRES_CLIENT_MAJOR}"
  docker compose "${COMPOSE_FILES[@]}" build \
    --pull \
    --build-arg "POSTGRES_CLIENT_MAJOR=${POSTGRES_CLIENT_MAJOR}" \
    sync-api

  log "Restarting sync-api only"
  docker compose "${COMPOSE_FILES[@]}" up -d --no-deps --force-recreate sync-api
}

verify_sync_api_client() {
  cd "$INSTALL_DIR/docker"
  log "Verifying PostgreSQL client versions inside sync-api"
  docker compose "${COMPOSE_FILES[@]}" exec -T sync-api pg_dump --version
  docker compose "${COMPOSE_FILES[@]}" exec -T sync-api pg_restore --version
  docker compose "${COMPOSE_FILES[@]}" exec -T sync-api psql --version
  local actual
  actual="$(docker compose "${COMPOSE_FILES[@]}" exec -T sync-api pg_dump --version | awk '{print $3}' | cut -d. -f1)"
  [ "$actual" = "$POSTGRES_CLIENT_MAJOR" ] ||
    fail "sync-api pg_dump major is ${actual}, expected ${POSTGRES_CLIENT_MAJOR}"
  log "sync-api PostgreSQL client repair completed"
}

main() {
  init_logging
  validate_client_major
  require_stack
  update_repo_if_requested
  build_and_restart_sync_api
  verify_sync_api_client
}

main "$@"
