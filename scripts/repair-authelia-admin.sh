#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${JRP_INSTALL_DIR:-/opt/jrp-supabase}"
DOCKER_DIR="${INSTALL_DIR}/docker"
AUTHELIA_DIR="${DOCKER_DIR}/volumes/authelia"
USERS_FILE="${AUTHELIA_DIR}/users_database.yml"
ADMIN_FILE="${DOCKER_DIR}/authelia-admin.generated.txt"
AUTH_DOMAIN="${AUTH_DOMAIN:-}"
BASE_DOMAIN="${BASE_DOMAIN:-}"
AUTHELIA_ADMIN_USER="${AUTHELIA_ADMIN_USER:-admin}"
AUTHELIA_ADMIN_EMAIL="${AUTHELIA_ADMIN_EMAIL:-}"
AUTHELIA_ADMIN_PASSWORD="${AUTHELIA_ADMIN_PASSWORD:-}"

log() {
  printf '[jrp-authelia-admin-repair] %s\n' "$*"
}

fail() {
  printf '[jrp-authelia-admin-repair] ERROR: %s\n' "$*" >&2
  exit 1
}

yaml_quote() {
  printf '%s' "$1" | sed "s/'/''/g; s/^/'/; s/$/'/"
}

main() {
  [ "$(id -u)" -eq 0 ] || fail "run as root"
  [ -d "$DOCKER_DIR" ] || fail "Docker stack directory not found: ${DOCKER_DIR}"
  [ -f "${AUTHELIA_DIR}/configuration.yml" ] || fail "Authelia configuration not found: ${AUTHELIA_DIR}/configuration.yml"
  [ -n "$AUTHELIA_ADMIN_PASSWORD" ] || fail "AUTHELIA_ADMIN_PASSWORD is required"
  [[ "$AUTHELIA_ADMIN_USER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$ ]] || fail "AUTHELIA_ADMIN_USER contains unsupported characters"
  [ "${#AUTHELIA_ADMIN_PASSWORD}" -ge 12 ] || fail "AUTHELIA_ADMIN_PASSWORD must be at least 12 characters"
  if [[ "$AUTHELIA_ADMIN_PASSWORD" =~ [[:space:]] ]]; then
    fail "AUTHELIA_ADMIN_PASSWORD must not contain whitespace"
  fi

  if [ -z "$BASE_DOMAIN" ] && [ -f "${DOCKER_DIR}/.env" ]; then
    BASE_DOMAIN="$(awk -F= '$1=="BASE_DOMAIN"{print $2; exit}' "${DOCKER_DIR}/.env" 2>/dev/null || true)"
  fi
  if [ -z "$AUTH_DOMAIN" ] && [ -f "${DOCKER_DIR}/.env" ]; then
    AUTH_DOMAIN="$(awk -F= '$1=="AUTH_DOMAIN"{print $2; exit}' "${DOCKER_DIR}/.env" 2>/dev/null || true)"
  fi
  if [ -z "$AUTHELIA_ADMIN_EMAIL" ]; then
    AUTHELIA_ADMIN_EMAIL="admin@${BASE_DOMAIN:-localhost}"
  fi

  local admin_hash
  admin_hash="$(openssl passwd -6 "$AUTHELIA_ADMIN_PASSWORD")"

  mkdir -p "$AUTHELIA_DIR"
  cat > "$USERS_FILE" <<EOF
users:
  ${AUTHELIA_ADMIN_USER}:
    displayname: "JRP Supabase Admin"
    password: $(yaml_quote "$admin_hash")
    email: $(yaml_quote "$AUTHELIA_ADMIN_EMAIL")
    groups:
      - studio-admins
EOF
  chmod 600 "$USERS_FILE"

  cat > "$ADMIN_FILE" <<EOF
Auth URL: https://${AUTH_DOMAIN}
Username: ${AUTHELIA_ADMIN_USER}
Password: ${AUTHELIA_ADMIN_PASSWORD}
Email: ${AUTHELIA_ADMIN_EMAIL}
EOF
  chmod 600 "$ADMIN_FILE"

  cd "$DOCKER_DIR"
  if docker ps --format '{{.Names}}' | grep -qx authelia; then
    docker restart authelia >/dev/null
    log "Restarted authelia"
  else
    docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d authelia
    log "Started authelia"
  fi

  log "Updated Authelia admin credentials at ${ADMIN_FILE}"
}

main "$@"
