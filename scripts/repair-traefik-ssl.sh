#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${JRP_INSTALL_DIR:-/opt/jrp-supabase}"
REPO_BRANCH="${JRP_REPO_BRANCH:-main}"
UPDATE_REPO="${UPDATE_REPO:-true}"
BASE_DOMAIN="${BASE_DOMAIN:-}"
API_DOMAIN="${API_DOMAIN:-}"
STUDIO_DOMAIN="${STUDIO_DOMAIN:-}"
AUTH_DOMAIN="${AUTH_DOMAIN:-}"
SYNC_API_DOMAIN="${SYNC_API_DOMAIN:-}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"
VERIFY_DNS="${VERIFY_DNS:-true}"
VERIFY_HTTPS="${VERIFY_HTTPS:-true}"
ENABLE_UFW="${ENABLE_UFW:-true}"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.traefik.yml)
ROUTED_SERVICES=(traefik studio authelia kong sync-api)
ROUTED_CONTAINER_NAMES=(traefik supabase-studio authelia supabase-kong sync-api)

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

update_repo() {
  if [ "$UPDATE_REPO" != "true" ]; then
    log "Skipping repo update because UPDATE_REPO=${UPDATE_REPO}"
    return
  fi
  if [ ! -d "$INSTALL_DIR/.git" ]; then
    log "Install dir is not a Git checkout; skipping repo update"
    return
  fi
  command -v git >/dev/null 2>&1 || fail "git is required when UPDATE_REPO=true"
  git -C "$INSTALL_DIR" fetch origin "$REPO_BRANCH"
  git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$REPO_BRANCH"
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

recreate_traefik_routes() {
  cd "$INSTALL_DIR/docker"
  cleanup_stale_compose_temp_containers
  docker compose "${COMPOSE_FILES[@]}" stop "${ROUTED_SERVICES[@]}" || true
  docker compose "${COMPOSE_FILES[@]}" rm -sf "${ROUTED_SERVICES[@]}" || true
  cleanup_stale_compose_temp_containers
  docker compose "${COMPOSE_FILES[@]}" up -d --build --force-recreate "${ROUTED_SERVICES[@]}"
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
  require_root
  update_repo
  require_stack
  configure_domains
  verify_compose_domains
  verify_dns_points_here
  configure_firewall
  recreate_traefik_routes
  wait_for_letsencrypt
  log "Traefik SSL repair completed"
}

main "$@"
