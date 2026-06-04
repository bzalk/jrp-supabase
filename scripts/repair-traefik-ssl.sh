#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${JRP_INSTALL_DIR:-/opt/jrp-supabase}"
REPO_BRANCH="${JRP_REPO_BRANCH:-main}"
UPDATE_REPO="${UPDATE_REPO:-false}"
BASE_DOMAIN="${BASE_DOMAIN:-}"
API_DOMAIN="${API_DOMAIN:-}"
STUDIO_DOMAIN="${STUDIO_DOMAIN:-}"
AUTH_DOMAIN="${AUTH_DOMAIN:-}"
SYNC_API_DOMAIN="${SYNC_API_DOMAIN:-}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"
VERIFY_DNS="${VERIFY_DNS:-true}"
VERIFY_HTTPS="${VERIFY_HTTPS:-true}"
ENABLE_UFW="${ENABLE_UFW:-true}"
REPAIR_STATUS_ONLY="${REPAIR_STATUS_ONLY:-false}"
REPAIR_LOG_DIR="${JRP_REPAIR_LOG_DIR:-${INSTALL_DIR}/docker/repair-logs}"
REPAIR_RUN_ID="${JRP_REPAIR_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
REPAIR_LOG_FILE="${REPAIR_LOG_DIR}/traefik-ssl-repair-${REPAIR_RUN_ID}.log"
REPAIR_LATEST_LOG="${REPAIR_LOG_DIR}/latest.log"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.traefik.yml)
ROUTED_SERVICES=(traefik studio authelia kong sync-api)
ROUTED_CONTAINER_NAMES=(traefik supabase-studio authelia supabase-kong sync-api)
REPO_UPDATED=false
FINAL_STATUS_CAPTURED=false

if [ -z "$BASE_DOMAIN" ] && [ -n "${1:-}" ]; then
  BASE_DOMAIN="$1"
fi

log() {
  printf '[jrp-traefik-repair] %s\n' "$*"
}

fail() {
  printf '[jrp-traefik-repair] ERROR: %s\n' "$*" >&2
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

read_env_value() {
  local file="$1"
  local key="$2"
  grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  local escaped
  escaped=$(printf '%s' "$value" | sed 's/[&|]/\\&/g')
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*$|${key}=${escaped}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "run as root"
  fi
}

require_stack() {
  [ -d "$INSTALL_DIR/docker" ] || fail "docker stack directory not found: ${INSTALL_DIR}/docker"
  [ -f "$INSTALL_DIR/docker/.env" ] || fail "environment file not found: ${INSTALL_DIR}/docker/.env"
  [ -f "$INSTALL_DIR/docker/docker-compose.yml" ] || fail "docker-compose.yml not found"
  [ -f "$INSTALL_DIR/docker/docker-compose.traefik.yml" ] || fail "docker-compose.traefik.yml not found; pull the latest repo first"
  command -v docker >/dev/null 2>&1 || fail "docker is not installed"
  docker compose version >/dev/null
  command -v openssl >/dev/null 2>&1 || fail "openssl is required"
  command -v curl >/dev/null 2>&1 || fail "curl is required"
}

validate_repo_branch() {
  if [ -z "$REPO_BRANCH" ] || [[ "$REPO_BRANCH" =~ [[:space:]] ]]; then
    fail "JRP_REPO_BRANCH must be a single branch name"
  fi
  git check-ref-format --branch "$REPO_BRANCH" >/dev/null ||
    fail "invalid JRP_REPO_BRANCH: ${REPO_BRANCH}"
}

acquire_repo_lock() {
  local lock_file="$INSTALL_DIR/.jrp-repo-update.lock"
  local lock_dir="$INSTALL_DIR/.jrp-repo-update.lockdir"
  local deadline

  if command -v flock >/dev/null 2>&1; then
    exec 9>"$lock_file"
    log "Waiting for repo update lock"
    flock -w 120 9 || fail "timed out waiting for repo update lock"
    return
  fi

  deadline=$((SECONDS + 120))
  until mkdir "$lock_dir" 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      fail "timed out waiting for repo update lock"
    fi
    sleep 2
  done
  trap 'rm -rf "$INSTALL_DIR/.jrp-repo-update.lockdir"' EXIT
}

release_repo_lock() {
  if command -v flock >/dev/null 2>&1; then
    flock -u 9 2>/dev/null || true
    exec 9>&- 2>/dev/null || true
  else
    rm -rf "$INSTALL_DIR/.jrp-repo-update.lockdir"
  fi
}

acquire_stack_lock() {
  local lock_file="$INSTALL_DIR/.jrp-stack-update.lock"
  local lock_dir="$INSTALL_DIR/.jrp-stack-update.lockdir"
  local deadline

  if command -v flock >/dev/null 2>&1; then
    exec 8>"$lock_file"
    log "Waiting for Docker Compose stack lock"
    flock -w 180 8 || fail "timed out waiting for Docker Compose stack lock"
    return
  fi

  deadline=$((SECONDS + 180))
  until mkdir "$lock_dir" 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      fail "timed out waiting for Docker Compose stack lock"
    fi
    sleep 2
  done
  trap 'rm -rf "$INSTALL_DIR/.jrp-stack-update.lockdir"' EXIT
}

update_repo() {
  local before after

  if [ "$UPDATE_REPO" != "true" ]; then
    log "Skipping repo update because UPDATE_REPO=${UPDATE_REPO}"
    return
  fi
  if [ ! -d "$INSTALL_DIR/.git" ]; then
    log "Install dir is not a Git checkout; skipping repo update"
    return
  fi
  command -v git >/dev/null 2>&1 || fail "git is required when UPDATE_REPO=true"
  validate_repo_branch
  acquire_repo_lock
  before="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || true)"
  git -C "$INSTALL_DIR" fetch --refmap= origin "refs/heads/${REPO_BRANCH}"
  if git -C "$INSTALL_DIR" rev-parse --verify --quiet "$REPO_BRANCH" >/dev/null; then
    git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
  else
    git -C "$INSTALL_DIR" checkout -b "$REPO_BRANCH" FETCH_HEAD
  fi
  git -C "$INSTALL_DIR" merge --ff-only FETCH_HEAD
  after="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || true)"
  release_repo_lock
  if [ -n "$before" ] && [ -n "$after" ] && [ "$before" != "$after" ]; then
    REPO_UPDATED=true
  fi
}

reexec_if_repo_updated() {
  local local_script="$INSTALL_DIR/scripts/repair-traefik-ssl.sh"

  if [ "$REPO_UPDATED" != "true" ]; then
    return
  fi
  if [ "${JRP_REPAIR_REEXECED:-false}" = "true" ]; then
    return
  fi
  if [ ! -f "$local_script" ]; then
    return
  fi

  log "Repo updated; re-running latest local repair script"
  export JRP_REPAIR_REEXECED=true
  exec bash "$local_script" "$@"
}

configure_domains() {
  local env_file="$INSTALL_DIR/docker/.env"

  if [ -n "$BASE_DOMAIN" ]; then
    API_DOMAIN="${API_DOMAIN:-supabase.${BASE_DOMAIN}}"
    STUDIO_DOMAIN="${STUDIO_DOMAIN:-studio.${BASE_DOMAIN}}"
    AUTH_DOMAIN="${AUTH_DOMAIN:-auth.${BASE_DOMAIN}}"
    SYNC_API_DOMAIN="${SYNC_API_DOMAIN:-sync-api.${BASE_DOMAIN}}"
    LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-admin@${BASE_DOMAIN}}"
  else
    API_DOMAIN="${API_DOMAIN:-$(read_env_value "$env_file" API_DOMAIN)}"
    STUDIO_DOMAIN="${STUDIO_DOMAIN:-$(read_env_value "$env_file" STUDIO_DOMAIN)}"
    AUTH_DOMAIN="${AUTH_DOMAIN:-$(read_env_value "$env_file" AUTH_DOMAIN)}"
    SYNC_API_DOMAIN="${SYNC_API_DOMAIN:-$(read_env_value "$env_file" SYNC_API_DOMAIN)}"
    LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-$(read_env_value "$env_file" LETSENCRYPT_EMAIL)}"
  fi

  [ -n "$API_DOMAIN" ] || fail "API_DOMAIN is not set and BASE_DOMAIN was not provided"
  [ -n "$STUDIO_DOMAIN" ] || fail "STUDIO_DOMAIN is not set and BASE_DOMAIN was not provided"
  [ -n "$AUTH_DOMAIN" ] || fail "AUTH_DOMAIN is not set and BASE_DOMAIN was not provided"
  [ -n "$SYNC_API_DOMAIN" ] || fail "SYNC_API_DOMAIN is not set and BASE_DOMAIN was not provided"
  [ -n "$LETSENCRYPT_EMAIL" ] || fail "LETSENCRYPT_EMAIL is not set"

  set_env_value "$env_file" API_DOMAIN "$API_DOMAIN"
  set_env_value "$env_file" STUDIO_DOMAIN "$STUDIO_DOMAIN"
  set_env_value "$env_file" AUTH_DOMAIN "$AUTH_DOMAIN"
  set_env_value "$env_file" SYNC_API_DOMAIN "$SYNC_API_DOMAIN"
  set_env_value "$env_file" LETSENCRYPT_EMAIL "$LETSENCRYPT_EMAIL"
  set_env_value "$env_file" SUPABASE_PUBLIC_URL "https://${API_DOMAIN}"
  set_env_value "$env_file" API_EXTERNAL_URL "https://${API_DOMAIN}"
  set_env_value "$env_file" SITE_URL "https://${STUDIO_DOMAIN}"
  set_env_value "$env_file" PROXY_DOMAIN "$API_DOMAIN"
}

compose_config() {
  cd "$INSTALL_DIR/docker"
  docker compose "${COMPOSE_FILES[@]}" config
}

verify_compose_domains() {
  local rendered expected domain
  rendered="$(compose_config)"
  for domain in "$API_DOMAIN" "$STUDIO_DOMAIN" "$AUTH_DOMAIN" "$SYNC_API_DOMAIN"; do
    expected="$(printf 'Host(`%s`)' "$domain")"
    if ! grep -qF "$expected" <<<"$rendered"; then
      fail "rendered Docker Compose config does not contain Traefik ${expected}. Check .env domain values."
    fi
  done
}

server_public_ips() {
  {
    curl -4fsS --max-time 10 https://api.ipify.org || true
    printf '\n'
    curl -6fsS --max-time 10 https://api64.ipify.org || true
    printf '\n'
  } | sed '/^$/d' | sort -u
}

domain_ips() {
  local domain="$1"
  getent ahosts "$domain" | awk '{print $1}' | sort -u
}

verify_dns_points_here() {
  if [ "$VERIFY_DNS" != "true" ]; then
    log "Skipping DNS verification because VERIFY_DNS=${VERIFY_DNS}"
    return
  fi

  local server_ips domain resolved matched ip
  server_ips="$(server_public_ips)"
  [ -n "$server_ips" ] || fail "could not determine this server's public IP address"

  for domain in "$API_DOMAIN" "$STUDIO_DOMAIN" "$AUTH_DOMAIN" "$SYNC_API_DOMAIN"; do
    resolved="$(domain_ips "$domain")"
    matched=false
    while IFS= read -r ip; do
      if grep -qx "$ip" <<<"$server_ips"; then
        matched=true
        break
      fi
    done <<<"$resolved"

    if [ "$matched" != "true" ]; then
      fail "DNS for ${domain} does not point to this VPS. Resolved: ${resolved:-none}. VPS IP(s): ${server_ips}."
    fi
  done
}

configure_firewall() {
  if [ "$ENABLE_UFW" != "true" ]; then
    log "Skipping UFW setup because ENABLE_UFW=${ENABLE_UFW}"
    return
  fi
  if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
  else
    log "ufw is not installed; skipping firewall update"
  fi
}

cleanup_stale_compose_temp_containers() {
  local id name prefix suffix removed=false

  while IFS=$'\t' read -r id name; do
    [ -n "$id" ] || continue
    prefix="${name%%_*}"
    suffix="${name#*_}"
    [ "$prefix" != "$name" ] || continue
    [[ "$prefix" =~ ^[0-9a-f]{12,}$ ]] || continue

    for candidate in "${ROUTED_CONTAINER_NAMES[@]}"; do
      if [ "$suffix" = "$candidate" ]; then
        log "Removing stale Docker Compose recreate container: ${name}"
        docker rm -f "$id" >/dev/null || true
        removed=true
        break
      fi
    done
  done < <(docker ps -a --format '{{.ID}}\t{{.Names}}')

  if [ "$removed" = "false" ]; then
    log "No stale Docker Compose recreate containers found"
  fi
}

remove_conflicting_route_containers() {
  local candidate removed=false

  for candidate in "${ROUTED_CONTAINER_NAMES[@]}"; do
    if docker container inspect "$candidate" >/dev/null 2>&1; then
      log "Removing conflicting route container: ${candidate}"
      docker rm -f "$candidate" >/dev/null || true
      removed=true
    fi
  done

  if [ "$removed" = "false" ]; then
    log "No exact route container name conflicts found"
  fi
}

wait_for_route_container_names_gone() {
  local deadline candidate waiting status
  deadline=$((SECONDS + 120))

  while [ "$SECONDS" -lt "$deadline" ]; do
    waiting=false
    for candidate in "${ROUTED_CONTAINER_NAMES[@]}"; do
      if docker container inspect "$candidate" >/dev/null 2>&1; then
        waiting=true
        status="$(docker inspect -f '{{.State.Status}}' "$candidate" 2>/dev/null || printf 'removing')"
        log "Waiting for route container name to be released: ${candidate} (${status})"
      fi
    done

    if [ "$waiting" = "false" ]; then
      return
    fi
    sleep 2
  done

  docker ps -a --filter 'name=^/(traefik|supabase-studio|authelia|supabase-kong|sync-api)$' \
    --format 'table {{.Names}}\t{{.Status}}\t{{.ID}}' || true
  fail "Timed out waiting for route container names to be released"
}

recreate_traefik_routes() {
  cd "$INSTALL_DIR/docker"
  acquire_stack_lock
  cleanup_stale_compose_temp_containers
  docker compose "${COMPOSE_FILES[@]}" stop "${ROUTED_SERVICES[@]}" || true
  docker compose "${COMPOSE_FILES[@]}" rm -sf "${ROUTED_SERVICES[@]}" || true
  remove_conflicting_route_containers
  wait_for_route_container_names_gone
  cleanup_stale_compose_temp_containers
  docker compose "${COMPOSE_FILES[@]}" up -d --build --force-recreate --no-deps "${ROUTED_SERVICES[@]}"
}

certificate_issuer() {
  local domain="$1"
  timeout 12 openssl s_client -connect "${domain}:443" -servername "$domain" </dev/null 2>/dev/null |
    openssl x509 -noout -issuer 2>/dev/null || true
}

certificate_subject() {
  local domain="$1"
  timeout 12 openssl s_client -connect "${domain}:443" -servername "$domain" </dev/null 2>/dev/null |
    openssl x509 -noout -subject 2>/dev/null || true
}

run_status_command() {
  local title="$1"
  shift

  printf '\n[jrp-traefik-repair] === %s ===\n' "$title"
  printf '[jrp-traefik-repair] $'
  printf ' %q' "$@"
  printf '\n'
  timeout 25 "$@" || true
}

log_domain_status() {
  local domain="$1"
  local resolved issuer subject

  resolved="$(domain_ips "$domain" | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
  issuer="$(certificate_issuer "$domain")"
  subject="$(certificate_subject "$domain")"
  log "Domain ${domain}: dns=${resolved:-none} subject=${subject:-none} issuer=${issuer:-none}"
  run_status_command "HTTPS strict probe ${domain}" curl -I --max-time 12 "https://${domain}/"
  run_status_command "HTTPS insecure probe ${domain}" curl -k -I --max-time 12 "https://${domain}/"
}

write_status_report() {
  local phase="$1"
  local domain

  printf '\n[jrp-traefik-repair] ######## STATUS REPORT: %s ########\n' "$phase"
  log "Timestamp UTC: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log "Install dir: ${INSTALL_DIR}"
  log "Repair run id: ${REPAIR_RUN_ID}"
  log "Repair log file: ${REPAIR_LOG_FILE}"
  log "Base domain: ${BASE_DOMAIN:-none}"
  log "API_DOMAIN=${API_DOMAIN:-none}"
  log "STUDIO_DOMAIN=${STUDIO_DOMAIN:-none}"
  log "AUTH_DOMAIN=${AUTH_DOMAIN:-none}"
  log "SYNC_API_DOMAIN=${SYNC_API_DOMAIN:-none}"
  log "VERIFY_DNS=${VERIFY_DNS} VERIFY_HTTPS=${VERIFY_HTTPS} ENABLE_UFW=${ENABLE_UFW} UPDATE_REPO=${UPDATE_REPO} REPAIR_STATUS_ONLY=${REPAIR_STATUS_ONLY}"
  log "Server public IPs: $(server_public_ips | tr '\n' ' ' | sed 's/[[:space:]]*$//')"

  run_status_command "Docker version" docker version
  run_status_command "Docker Compose version" docker compose version
  run_status_command "Listening TCP ports" sh -c "ss -ltnp 2>/dev/null | grep -E ':(80|443|8000|8443|8080|9091|5432)\\b' || true"

  if [ -d "$INSTALL_DIR/docker" ]; then
    (
      cd "$INSTALL_DIR/docker"
      run_status_command "Docker Compose ps" docker compose "${COMPOSE_FILES[@]}" ps -a
      run_status_command "Rendered Traefik labels" sh -c "docker compose ${COMPOSE_FILES[*]} config 2>/dev/null | grep -E 'traefik\\.http\\.(routers|services|middlewares)' || true"
    )
  fi

  run_status_command "All containers" docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}'
  for domain in "$API_DOMAIN" "$STUDIO_DOMAIN" "$AUTH_DOMAIN" "$SYNC_API_DOMAIN"; do
    [ -n "$domain" ] && log_domain_status "$domain"
  done

  for container in traefik authelia supabase-kong supabase-studio sync-api supabase-analytics supabase-db; do
    run_status_command "Inspect ${container}" docker inspect -f 'name={{.Name}} status={{.State.Status}} running={{.State.Running}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restart_count={{.RestartCount}} image={{.Config.Image}}' "$container"
  done

  for container in traefik authelia supabase-kong supabase-studio sync-api supabase-analytics; do
    run_status_command "Recent logs ${container}" docker logs --tail 120 "$container"
  done
  printf '[jrp-traefik-repair] ######## END STATUS REPORT: %s ########\n\n' "$phase"
}

publish_log_locations() {
  log "Full repair log: ${REPAIR_LOG_FILE}"
  if [ -n "${SYNC_API_DOMAIN:-}" ]; then
    log "Repair log URL when Sync API is reachable: https://${SYNC_API_DOMAIN}/v1/repair-logs/latest"
    log "This run log URL when Sync API is reachable: https://${SYNC_API_DOMAIN}/v1/repair-logs/$(basename "$REPAIR_LOG_FILE")"
  fi
}

on_exit() {
  local status="$1"
  if [ "$status" -ne 0 ] && [ "$FINAL_STATUS_CAPTURED" != "true" ]; then
    FINAL_STATUS_CAPTURED=true
    log "Repair failed with exit code ${status}; collecting final status"
    write_status_report "failure"
    log "Full repair log: ${REPAIR_LOG_FILE}"
    if [ -n "${SYNC_API_DOMAIN:-}" ]; then
      log "Repair log URL when Sync API is reachable: https://${SYNC_API_DOMAIN}/v1/repair-logs/latest"
    fi
  fi
}

wait_for_letsencrypt() {
  if [ "$VERIFY_HTTPS" != "true" ]; then
    log "Skipping HTTPS certificate verification because VERIFY_HTTPS=${VERIFY_HTTPS}"
    return
  fi

  local deadline domain issuer all_ok
  deadline=$((SECONDS + 360))
  while [ "$SECONDS" -lt "$deadline" ]; do
    all_ok=true
    for domain in "$API_DOMAIN" "$STUDIO_DOMAIN" "$AUTH_DOMAIN" "$SYNC_API_DOMAIN"; do
      issuer="$(certificate_issuer "$domain")"
      if ! grep -Eiq "Let.s Encrypt|ISRG Root|R[0-9]+|E[0-9]+" <<<"$issuer"; then
        all_ok=false
        break
      fi
    done
    if [ "$all_ok" = "true" ]; then
      log "Let's Encrypt certificates are active"
      return
    fi
    sleep 10
  done

  for domain in "$API_DOMAIN" "$STUDIO_DOMAIN" "$AUTH_DOMAIN" "$SYNC_API_DOMAIN"; do
    log "${domain} $(certificate_subject "$domain") $(certificate_issuer "$domain")"
  done
  docker logs --tail 200 traefik || true
  fail "Traefik did not obtain Let's Encrypt certificates within 360 seconds. Check DNS, ports 80/443, and Traefik logs."
}

main() {
  cd /
  require_root
  init_logging
  trap 'on_exit $?' EXIT
  update_repo
  reexec_if_repo_updated "$@"
  require_stack
  configure_domains
  publish_log_locations
  verify_compose_domains
  write_status_report "preflight"
  if [ "$REPAIR_STATUS_ONLY" = "true" ]; then
    log "Status-only mode completed; no repair actions were attempted"
    return
  fi
  verify_dns_points_here
  configure_firewall
  recreate_traefik_routes
  wait_for_letsencrypt
  write_status_report "success"
  log "Traefik SSL repair completed"
}

main "$@"
