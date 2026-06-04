#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${JRP_REPO_URL:-https://github.com/bzalk/jrp-supabase.git}"
REPO_BRANCH="${JRP_REPO_BRANCH:-main}"
INSTALL_DIR="${JRP_INSTALL_DIR:-/opt/jrp-supabase}"
BASE_DOMAIN="${BASE_DOMAIN:-}"
RUN_INSTALL="${RUN_INSTALL:-true}"
CONFIRM_RESET="${CONFIRM_RESET:-}"
RESET_LOG_FILE="${RESET_LOG_FILE:-/root/jrp-reset-vps-$(date -u +%Y%m%dT%H%M%SZ).log}"
PRUNE_DOCKER_SYSTEM="${PRUNE_DOCKER_SYSTEM:-false}"

KNOWN_CONTAINERS=(
  traefik
  authelia
  sync-api
  supabase-studio
  supabase-kong
  supabase-auth
  supabase-rest
  realtime-dev.supabase-realtime
  supabase-storage
  supabase-imgproxy
  supabase-meta
  supabase-edge-functions
  supabase-analytics
  supabase-db
  supabase-vector
  supabase-pooler
  supabase-caddy
  supabase-nginx
)

KNOWN_CONTAINER_SUFFIXES=(
  traefik
  authelia
  sync-api
  supabase-studio
  supabase-kong
  supabase-auth
  supabase-rest
  realtime-dev.supabase-realtime
  supabase-storage
  supabase-imgproxy
  supabase-meta
  supabase-edge-functions
  supabase-analytics
  supabase-db
  supabase-vector
  supabase-pooler
)

KNOWN_IMAGES=(
  supabase-sync-api:latest
  supabase-studio-mcp:local
)

if [ -z "$BASE_DOMAIN" ] && [ -n "${1:-}" ]; then
  BASE_DOMAIN="$1"
fi

log() {
  printf '[jrp-reset] %s\n' "$*"
}

fail() {
  printf '[jrp-reset] ERROR: %s\n' "$*" >&2
  exit 1
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "run as root"
  fi
}

confirm_reset() {
  if [ "$CONFIRM_RESET" != "RESET" ]; then
    cat >&2 <<EOF
[jrp-reset] This is destructive.
[jrp-reset] It removes the local JRP Supabase checkout, stack containers,
[jrp-reset] project Docker volumes, and local bind-mounted data under:
[jrp-reset]   ${INSTALL_DIR}
[jrp-reset]
[jrp-reset] Re-run with:
[jrp-reset]   CONFIRM_RESET=RESET
EOF
    exit 2
  fi
}

install_minimum_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y curl ca-certificates
}

install_docker_if_missing() {
  if command -v docker >/dev/null 2>&1; then
    systemctl start docker || true
    return
  fi
  curl -fsSL https://get.docker.com | sh
  systemctl enable docker
  systemctl start docker
}

compose_down_if_possible() {
  if [ ! -d "$INSTALL_DIR/docker" ] || ! command -v docker >/dev/null 2>&1; then
    return
  fi

  cd "$INSTALL_DIR/docker"
  if [ -f docker-compose.yml ] && [ -f docker-compose.traefik.yml ]; then
    log "Stopping stack with Traefik compose files"
    docker compose -f docker-compose.yml -f docker-compose.traefik.yml down -v --remove-orphans || true
  fi
  if [ -f docker-compose.yml ]; then
    log "Stopping stack with base compose file"
    docker compose -f docker-compose.yml down -v --remove-orphans || true
  fi
}

remove_known_containers() {
  local id name prefix suffix candidate

  if ! command -v docker >/dev/null 2>&1; then
    return
  fi

  for candidate in "${KNOWN_CONTAINERS[@]}"; do
    if docker container inspect "$candidate" >/dev/null 2>&1; then
      log "Removing container ${candidate}"
      docker rm -f "$candidate" >/dev/null || true
    fi
  done

  while IFS=$'\t' read -r id name; do
    [ -n "$id" ] || continue
    prefix="${name%%_*}"
    suffix="${name#*_}"
    [ "$prefix" != "$name" ] || continue
    [[ "$prefix" =~ ^[0-9a-f]{12,}$ ]] || continue
    for candidate in "${KNOWN_CONTAINER_SUFFIXES[@]}"; do
      if [ "$suffix" = "$candidate" ]; then
        log "Removing stale Compose temp container ${name}"
        docker rm -f "$id" >/dev/null || true
        break
      fi
    done
  done < <(docker ps -a --format '{{.ID}}\t{{.Names}}')
}

wait_for_known_containers_gone() {
  local deadline candidate waiting status
  deadline=$((SECONDS + 180))

  while [ "$SECONDS" -lt "$deadline" ]; do
    waiting=false
    for candidate in "${KNOWN_CONTAINERS[@]}"; do
      if docker container inspect "$candidate" >/dev/null 2>&1; then
        waiting=true
        status="$(docker inspect -f '{{.State.Status}}' "$candidate" 2>/dev/null || printf 'removing')"
        log "Waiting for container name to be released: ${candidate} (${status})"
      fi
    done
    if [ "$waiting" = "false" ]; then
      return
    fi
    sleep 2
  done

  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.ID}}' || true
  fail "Timed out waiting for stack container names to be released"
}

remove_project_docker_resources() {
  local volume network image

  if ! command -v docker >/dev/null 2>&1; then
    return
  fi

  for volume in $(docker volume ls --format '{{.Name}}' | grep -E '^(supabase_|jrp-supabase_)' || true); do
    log "Removing Docker volume ${volume}"
    docker volume rm -f "$volume" >/dev/null || true
  done

  for network in $(docker network ls --format '{{.Name}}' | grep -E '^(supabase_|jrp-supabase_)' || true); do
    log "Removing Docker network ${network}"
    docker network rm "$network" >/dev/null || true
  done

  for image in "${KNOWN_IMAGES[@]}"; do
    if docker image inspect "$image" >/dev/null 2>&1; then
      log "Removing image ${image}"
      docker image rm -f "$image" >/dev/null || true
    fi
  done

  if [ "$PRUNE_DOCKER_SYSTEM" = "true" ]; then
    log "Pruning unused Docker resources because PRUNE_DOCKER_SYSTEM=true"
    docker system prune -af --volumes || true
  fi
}

remove_local_files() {
  log "Removing install directory ${INSTALL_DIR}"
  rm -rf "$INSTALL_DIR"
  rm -f /root/jrp-generated-secrets.log /root/jrp-install-vps.sh
}

run_fresh_install() {
  if [ "$RUN_INSTALL" != "true" ]; then
    log "Skipping fresh install because RUN_INSTALL=${RUN_INSTALL}"
    return
  fi
  [ -n "$BASE_DOMAIN" ] || fail "BASE_DOMAIN is required to run the fresh install"

  export BASE_DOMAIN
  export JRP_REPO_URL="$REPO_URL"
  export JRP_REPO_BRANCH="$REPO_BRANCH"
  export JRP_INSTALL_DIR="$INSTALL_DIR"

  log "Downloading installer from read-only repo source ${REPO_URL}#${REPO_BRANCH}"
  curl -fsSL "https://raw.githubusercontent.com/bzalk/jrp-supabase/${REPO_BRANCH}/scripts/install-vps.sh" \
    -o /tmp/jrp-install-vps.sh
  chmod +x /tmp/jrp-install-vps.sh
  /tmp/jrp-install-vps.sh "$BASE_DOMAIN"
}

main() {
  exec > >(tee -a "$RESET_LOG_FILE") 2>&1
  require_root
  confirm_reset
  log "Reset log: ${RESET_LOG_FILE}"
  log "GitHub is used as a read-only source for downloading/cloning scripts."
  install_minimum_packages
  install_docker_if_missing
  compose_down_if_possible
  remove_known_containers
  wait_for_known_containers_gone
  remove_project_docker_resources
  remove_local_files
  run_fresh_install
  log "Reset completed"
}

main "$@"
