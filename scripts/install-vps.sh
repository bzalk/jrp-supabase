#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${JRP_REPO_URL:-https://github.com/bzalk/jrp-supabase.git}"
REPO_BRANCH="${JRP_REPO_BRANCH:-main}"
INSTALL_DIR="${JRP_INSTALL_DIR:-/opt/jrp-supabase}"
BASE_DOMAIN="${BASE_DOMAIN:-}"
API_DOMAIN="${API_DOMAIN:-}"
STUDIO_DOMAIN="${STUDIO_DOMAIN:-}"
AUTH_DOMAIN="${AUTH_DOMAIN:-}"
SYNC_API_DOMAIN="${SYNC_API_DOMAIN:-}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"
PROJECT_NAME="${PROJECT_NAME:-JRP Supabase}"
ORG_NAME="${ORG_NAME:-Jamrock Partners}"
ENABLE_UFW="${ENABLE_UFW:-true}"
START_STACK="${START_STACK:-true}"
FORCE_REGENERATE_SECRETS="${FORCE_REGENERATE_SECRETS:-false}"

log() {
  printf '[jrp-install] %s\n' "$*"
}

fail() {
  printf '[jrp-install] ERROR: %s\n' "$*" >&2
  exit 1
}

random_hex() {
  openssl rand -hex "$1"
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

read_env_value() {
  local file="$1"
  local key="$2"
  grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

set_secret_if_unset_or_placeholder() {
  local file="$1"
  local key="$2"
  local value="$3"
  local current
  current="$(read_env_value "$file" "$key")"
  case "$current" in
    ""|change-me-*|your-*|this_password_is_insecure_and_should_be_updated|secret1234)
      set_env_value "$file" "$key" "$value"
      ;;
  esac
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "run as root"
  fi
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y git curl ca-certificates gnupg ufw openssl
}

install_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable docker
  systemctl start docker
  docker compose version >/dev/null
}

checkout_repo() {
  mkdir -p "$INSTALL_DIR"
  if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" fetch origin "$REPO_BRANCH"
    git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$REPO_BRANCH"
  else
    git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
}

configure_env() {
  [ -n "$BASE_DOMAIN" ] || fail "BASE_DOMAIN is required, for example BASE_DOMAIN=example.com"

  API_DOMAIN="${API_DOMAIN:-supabase.${BASE_DOMAIN}}"
  STUDIO_DOMAIN="${STUDIO_DOMAIN:-studio.${BASE_DOMAIN}}"
  AUTH_DOMAIN="${AUTH_DOMAIN:-auth.${BASE_DOMAIN}}"
  SYNC_API_DOMAIN="${SYNC_API_DOMAIN:-sync-api.${BASE_DOMAIN}}"
  LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-admin@${BASE_DOMAIN}}"

  cd "$INSTALL_DIR/docker"
  created_env=false
  if [ ! -f .env ]; then
    cp .env.example .env
    created_env=true
  fi

  jwt_secret="$(read_env_value .env JWT_SECRET)"
  if [ "$created_env" = "true" ] || [ "$FORCE_REGENERATE_SECRETS" = "true" ] || [[ "$jwt_secret" == your-* ]]; then
    sh ./utils/generate-keys.sh --update-env >/root/jrp-generated-secrets.log
  else
    log "Preserving existing generated Supabase secrets in ${INSTALL_DIR}/docker/.env"
  fi

  set_env_value .env API_DOMAIN "$API_DOMAIN"
  set_env_value .env STUDIO_DOMAIN "$STUDIO_DOMAIN"
  set_env_value .env AUTH_DOMAIN "$AUTH_DOMAIN"
  set_env_value .env SYNC_API_DOMAIN "$SYNC_API_DOMAIN"
  set_env_value .env LETSENCRYPT_EMAIL "$LETSENCRYPT_EMAIL"
  set_env_value .env SUPABASE_PUBLIC_URL "https://${API_DOMAIN}"
  set_env_value .env API_EXTERNAL_URL "https://${API_DOMAIN}"
  set_env_value .env SITE_URL "https://${STUDIO_DOMAIN}"
  set_env_value .env PROXY_DOMAIN "$API_DOMAIN"
  set_env_value .env STUDIO_DEFAULT_PROJECT "$PROJECT_NAME"
  set_env_value .env STUDIO_DEFAULT_ORGANIZATION "$ORG_NAME"
  set_secret_if_unset_or_placeholder .env POOLER_TENANT_ID "$(random_hex 8)"
  set_secret_if_unset_or_placeholder .env SYNC_API_TOKEN "$(random_hex 32)"
  set_secret_if_unset_or_placeholder .env AUTHELIA_SESSION_SECRET "$(random_hex 32)"
  set_secret_if_unset_or_placeholder .env AUTHELIA_STORAGE_ENCRYPTION_KEY "$(random_hex 32)"

  mkdir -p branches volumes/functions volumes/snippets volumes/storage volumes/authelia
  chmod 600 .env
}

start_stack() {
  cd "$INSTALL_DIR/docker"
  docker compose -f docker-compose.yml -f docker-compose.traefik.yml pull --ignore-pull-failures || true
  docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build
}

configure_firewall() {
  if [ "$ENABLE_UFW" != "true" ]; then
    log "Skipping UFW setup because ENABLE_UFW=${ENABLE_UFW}"
    return
  fi
  ufw allow OpenSSH
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
}

write_summary() {
  cat >"${INSTALL_DIR}/install-summary.txt" <<EOF
JRP Supabase install complete.

Install dir: ${INSTALL_DIR}
API:        https://${API_DOMAIN}
Studio:     https://${STUDIO_DOMAIN}
Auth:       https://${AUTH_DOMAIN}
Sync API:   https://${SYNC_API_DOMAIN}

Secrets are stored in:
${INSTALL_DIR}/docker/.env

Generated secret log:
/root/jrp-generated-secrets.log

Lifecycle:
cd ${INSTALL_DIR}/docker
docker compose -f docker-compose.yml -f docker-compose.traefik.yml ps
docker compose -f docker-compose.yml -f docker-compose.traefik.yml logs -f sync-api
EOF
  cat "${INSTALL_DIR}/install-summary.txt"
}

main() {
  require_root
  log "Installing OS packages"
  install_packages
  log "Installing Docker"
  install_docker
  log "Checking out ${REPO_URL}#${REPO_BRANCH} into ${INSTALL_DIR}"
  checkout_repo
  log "Configuring stack for ${BASE_DOMAIN}"
  configure_env
  configure_firewall
  if [ "$START_STACK" = "true" ]; then
    log "Starting Supabase, Sync API, and Traefik"
    start_stack
  else
    log "Skipping stack start because START_STACK=${START_STACK}"
  fi
  write_summary
}

main "$@"
