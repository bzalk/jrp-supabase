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
LETSENCRYPT_CA_SERVER="${LETSENCRYPT_CA_SERVER:-}"
LETSENCRYPT_STAGING="${LETSENCRYPT_STAGING:-false}"
LETSENCRYPT_PRODUCTION="${LETSENCRYPT_PRODUCTION:-false}"
PROJECT_NAME="${PROJECT_NAME:-JRP Supabase}"
ORG_NAME="${ORG_NAME:-Jamrock Partners}"
ENABLE_UFW="${ENABLE_UFW:-true}"
START_STACK="${START_STACK:-true}"
FORCE_REGENERATE_SECRETS="${FORCE_REGENERATE_SECRETS:-false}"
VERIFY_DNS="${VERIFY_DNS:-true}"
VERIFY_HTTPS="${VERIFY_HTTPS:-true}"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.traefik.yml)
COMPOSE_TEMP_CONTAINER_NAMES=(
  traefik
  supabase-studio
  authelia
  supabase-kong
  sync-api
)
REPO_UPDATED=false

if [ -z "$BASE_DOMAIN" ] && [ -n "${1:-}" ]; then
  BASE_DOMAIN="$1"
fi

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

validate_sync_api_token() {
  local token="$1"

  [ -n "$token" ] || fail "SYNC_API_TOKEN is required. Generate it in the control plane/UX and pass it to the installer."
  [ "${#token}" -ge 32 ] || fail "SYNC_API_TOKEN must be at least 32 characters"
  if [[ "$token" =~ [[:space:]] ]]; then
    fail "SYNC_API_TOKEN must not contain whitespace"
  fi
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
  ensure_supported_docker_engine
  systemctl enable docker
  systemctl start docker
  docker compose version >/dev/null
}

ensure_supported_docker_engine() {
  local version major pinned_version

  version="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
  major="${version%%.*}"

  if [ -n "$major" ] && [ "$major" -lt 29 ] 2>/dev/null; then
    return
  fi

  log "Docker Engine ${version:-unknown} is not compatible with the bundled Traefik Docker provider; installing latest Docker 28.x"
  apt-get update
  pinned_version="$(apt-cache madison docker-ce | awk '{print $3}' | grep -E '^5:28\.' | head -n 1 || true)"
  if [ -z "$pinned_version" ]; then
    fail "Could not find a Docker 28.x package in the configured apt repositories"
  fi

  systemctl stop docker 2>/dev/null || true
  apt-get install -y --allow-downgrades \
    "docker-ce=${pinned_version}" \
    "docker-ce-cli=${pinned_version}" \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin
  apt-mark hold docker-ce docker-ce-cli >/dev/null || true
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

checkout_repo() {
  local before after

  mkdir -p "$INSTALL_DIR"
  validate_repo_branch
  if [ -d "$INSTALL_DIR/.git" ]; then
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
  else
    git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
}

reexec_if_repo_updated() {
  local local_script="$INSTALL_DIR/scripts/install-vps.sh"

  if [ "$REPO_UPDATED" != "true" ]; then
    return
  fi
  if [ "${JRP_INSTALL_REEXECED:-false}" = "true" ]; then
    return
  fi
  if [ ! -f "$local_script" ]; then
    return
  fi

  log "Repo updated; re-running latest local installer script"
  export JRP_INSTALL_REEXECED=true
  exec bash "$local_script" "$@"
}

configure_env() {
  [ -n "$BASE_DOMAIN" ] || fail "BASE_DOMAIN is required, for example BASE_DOMAIN=example.com"

  API_DOMAIN="${API_DOMAIN:-supabase.${BASE_DOMAIN}}"
  STUDIO_DOMAIN="${STUDIO_DOMAIN:-studio.${BASE_DOMAIN}}"
  AUTH_DOMAIN="${AUTH_DOMAIN:-auth.${BASE_DOMAIN}}"
  SYNC_API_DOMAIN="${SYNC_API_DOMAIN:-sync-api.${BASE_DOMAIN}}"
  LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-admin@${BASE_DOMAIN}}"
  if [ -z "$LETSENCRYPT_CA_SERVER" ]; then
    if [ "$LETSENCRYPT_PRODUCTION" = "true" ] && [ "$LETSENCRYPT_STAGING" != "true" ]; then
      LETSENCRYPT_CA_SERVER="https://acme-v02.api.letsencrypt.org/directory"
    else
      LETSENCRYPT_CA_SERVER="https://acme-staging-v02.api.letsencrypt.org/directory"
    fi
  fi
  if [ "$LETSENCRYPT_CA_SERVER" = "https://acme-staging-v02.api.letsencrypt.org/directory" ]; then
    log "Using Let's Encrypt staging CA for testing: ${LETSENCRYPT_CA_SERVER}"
  else
    log "Using Let's Encrypt production CA because LETSENCRYPT_PRODUCTION=${LETSENCRYPT_PRODUCTION}: ${LETSENCRYPT_CA_SERVER}"
  fi

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

  validate_sync_api_token "${SYNC_API_TOKEN:-}"
  set_env_value .env SYNC_API_TOKEN "$SYNC_API_TOKEN"

  set_env_value .env API_DOMAIN "$API_DOMAIN"
  set_env_value .env STUDIO_DOMAIN "$STUDIO_DOMAIN"
  set_env_value .env AUTH_DOMAIN "$AUTH_DOMAIN"
  set_env_value .env SYNC_API_DOMAIN "$SYNC_API_DOMAIN"
  set_env_value .env LETSENCRYPT_EMAIL "$LETSENCRYPT_EMAIL"
  set_env_value .env LETSENCRYPT_CA_SERVER "$LETSENCRYPT_CA_SERVER"
  set_env_value .env LETSENCRYPT_PRODUCTION "$LETSENCRYPT_PRODUCTION"
  set_env_value .env SUPABASE_PUBLIC_URL "https://${API_DOMAIN}"
  set_env_value .env API_EXTERNAL_URL "https://${API_DOMAIN}"
  set_env_value .env SITE_URL "https://${STUDIO_DOMAIN}"
  set_env_value .env PROXY_DOMAIN "$API_DOMAIN"
  set_env_value .env POSTGRES_LOG_MIN_MESSAGES "warning"
  set_env_value .env STUDIO_DEFAULT_PROJECT "$PROJECT_NAME"
  set_env_value .env STUDIO_DEFAULT_ORGANIZATION "$ORG_NAME"
  set_secret_if_unset_or_placeholder .env POOLER_TENANT_ID "$(random_hex 8)"
  set_secret_if_unset_or_placeholder .env AUTHELIA_SESSION_SECRET "$(random_hex 32)"
  set_secret_if_unset_or_placeholder .env AUTHELIA_STORAGE_ENCRYPTION_KEY "$(random_hex 32)"

  mkdir -p branches repair-logs volumes/functions volumes/snippets volumes/storage volumes/authelia
  chmod 600 .env
}

configure_authelia() {
  cd "$INSTALL_DIR/docker"

  local authelia_dir admin_file config_file users_file notification_file
  local admin_user admin_email admin_password admin_hash session_cookie_name
  local session_secret storage_key

  authelia_dir="$INSTALL_DIR/docker/volumes/authelia"
  admin_file="$INSTALL_DIR/docker/authelia-admin.generated.txt"
  config_file="$authelia_dir/configuration.yml"
  users_file="$authelia_dir/users_database.yml"
  notification_file="$authelia_dir/notification.txt"

  mkdir -p "$authelia_dir"

  if [ -f "$config_file" ] && [ -f "$users_file" ] && [ "$FORCE_REGENERATE_SECRETS" != "true" ]; then
    log "Preserving existing Authelia configuration in ${authelia_dir}"
    return
  fi

  admin_user="${AUTHELIA_ADMIN_USER:-admin}"
  admin_email="${AUTHELIA_ADMIN_EMAIL:-admin@${BASE_DOMAIN}}"
  admin_password="${AUTHELIA_ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -d '\n')}"
  admin_hash="$(openssl passwd -6 "$admin_password")"
  session_cookie_name="authelia_session_$(printf '%s' "$BASE_DOMAIN" | tr '.-' '__')"
  session_secret="$(read_env_value .env AUTHELIA_SESSION_SECRET)"
  storage_key="$(read_env_value .env AUTHELIA_STORAGE_ENCRYPTION_KEY)"

  cat > "$config_file" <<EOF
theme: auto
default_2fa_method: totp

server:
  endpoints:
    authz:
      forward-auth:
        implementation: ForwardAuth

log:
  level: info

totp:
  issuer: ${BASE_DOMAIN}

authentication_backend:
  password_reset:
    disable: true
  password_change:
    disable: true
  file:
    path: /config/users_database.yml
    watch: true
    search:
      email: true
      case_insensitive: true
    password:
      algorithm: sha2crypt
      sha2crypt:
        variant: sha512
        iterations: 50000

access_control:
  default_policy: deny
  rules:
    - domain: ${AUTH_DOMAIN}
      policy: bypass
    - domain: ${STUDIO_DOMAIN}
      subject:
        - group:studio-admins
      policy: one_factor

session:
  secret: ${session_secret}
  name: ${session_cookie_name}
  same_site: lax
  expiration: 12h
  inactivity: 45m
  remember_me: 14d
  cookies:
    - domain: ${BASE_DOMAIN}
      authelia_url: https://${AUTH_DOMAIN}
      default_redirection_url: https://${STUDIO_DOMAIN}

regulation:
  max_retries: 5
  find_time: 2m
  ban_time: 15m

storage:
  encryption_key: ${storage_key}
  local:
    path: /config/db.sqlite3

notifier:
  filesystem:
    filename: /config/notification.txt
EOF

  cat > "$users_file" <<EOF
users:
  ${admin_user}:
    displayname: "JRP Supabase Admin"
    password: "${admin_hash}"
    email: ${admin_email}
    groups:
      - studio-admins
EOF

  : > "$notification_file"
  chmod 600 "$config_file" "$users_file"

  cat > "$admin_file" <<EOF
Auth URL: https://${AUTH_DOMAIN}
Username: ${admin_user}
Password: ${admin_password}
Email: ${admin_email}
EOF
  chmod 600 "$admin_file"
  log "Generated Authelia configuration and admin credentials at ${admin_file}"
}

compose_config() {
  cd "$INSTALL_DIR/docker"
  docker compose "${COMPOSE_FILES[@]}" config
}

verify_compose_domains() {
  local rendered
  rendered="$(compose_config)"
  for domain in "$API_DOMAIN" "$STUDIO_DOMAIN" "$AUTH_DOMAIN" "$SYNC_API_DOMAIN"; do
    local expected
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

  local server_ips
  server_ips="$(server_public_ips)"
  [ -n "$server_ips" ] || fail "could not determine this server's public IP address"

  for domain in "$API_DOMAIN" "$STUDIO_DOMAIN" "$AUTH_DOMAIN" "$SYNC_API_DOMAIN"; do
    local resolved matched
    resolved="$(domain_ips "$domain")"
    matched=false
    while IFS= read -r ip; do
      if grep -qx "$ip" <<<"$server_ips"; then
        matched=true
        break
      fi
    done <<<"$resolved"

    if [ "$matched" != "true" ]; then
      fail "DNS for ${domain} does not point to this VPS. Resolved: ${resolved:-none}. VPS IP(s): ${server_ips}. Fix DNS before installing TLS."
    fi
  done
}

cleanup_stale_compose_temp_containers() {
  local id name prefix suffix removed=false

  while IFS=$'\t' read -r id name; do
    [ -n "$id" ] || continue
    prefix="${name%%_*}"
    suffix="${name#*_}"
    [ "$prefix" != "$name" ] || continue
    [[ "$prefix" =~ ^[0-9a-f]{12,}$ ]] || continue

    for candidate in "${COMPOSE_TEMP_CONTAINER_NAMES[@]}"; do
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

  for candidate in "${COMPOSE_TEMP_CONTAINER_NAMES[@]}"; do
    if docker container inspect "$candidate" >/dev/null 2>&1; then
      log "Removing route container before full stack recreate: ${candidate}"
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
    for candidate in "${COMPOSE_TEMP_CONTAINER_NAMES[@]}"; do
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

collect_startup_diagnostics() {
  cd "$INSTALL_DIR/docker"
  log "Collecting startup diagnostics"
  docker compose "${COMPOSE_FILES[@]}" ps -a || true
  docker inspect -f 'name={{.Name}} status={{.State.Status}} running={{.State.Running}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} exit_code={{.State.ExitCode}} error={{.State.Error}} restart_count={{.RestartCount}}' supabase-db || true
  docker inspect -f '{{range .State.Health.Log}}{{println .Start .End .ExitCode .Output}}{{end}}' supabase-db || true
  for container in supabase-db supabase-analytics supabase-auth supabase-rest supabase-storage supabase-pooler authelia supabase-studio supabase-kong sync-api traefik; do
    log "Logs for ${container}"
    docker logs --tail 180 "$container" || true
  done
}

wait_for_db_healthy() {
  local deadline health
  deadline=$((SECONDS + 180))

  while [ "$SECONDS" -lt "$deadline" ]; do
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' supabase-db 2>/dev/null || true)"
    if [ "$health" = "healthy" ]; then
      return
    fi
    log "Waiting for supabase-db to become healthy (${health:-not-created})"
    sleep 3
  done

  collect_startup_diagnostics
  fail "Timed out waiting for supabase-db to become healthy"
}

repair_db_roles() {
  cd "$INSTALL_DIR/docker"

  local attempt password role
  password="$(read_env_value .env POSTGRES_PASSWORD)"
  [ -n "$password" ] || fail "POSTGRES_PASSWORD is empty; cannot repair database roles"

  for attempt in $(seq 1 60); do
    log "Ensuring Supabase internal database roles use the generated Postgres password (attempt ${attempt})"
    if docker exec -i -e PGPASSWORD="$password" supabase-db \
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
    EXECUTE format(
      'CREATE ROLE supabase_admin WITH LOGIN SUPERUSER CREATEDB CREATEROLE REPLICATION BYPASSRLS PASSWORD %L',
      pgpass
    );
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
    then
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
      for role in supabase_admin authenticator supabase_auth_admin supabase_storage_admin; do
        docker exec -e PGPASSWORD="$password" supabase-db \
          psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -h localhost -U "$role" -d postgres -c 'select 1' >/dev/null
      done
      log "Supabase internal database roles verified with generated password"
      return
    fi
    sleep 3
  done

  collect_startup_diagnostics
  fail "Timed out repairing Supabase internal database roles"
}

restart_db_client_services() {
  cd "$INSTALL_DIR/docker"
  docker compose "${COMPOSE_FILES[@]}" restart \
    auth rest storage realtime meta analytics supavisor studio kong sync-api >/dev/null || true
}

start_stack() {
  cd "$INSTALL_DIR/docker"
  acquire_stack_lock
  docker compose "${COMPOSE_FILES[@]}" pull --ignore-pull-failures || true
  cleanup_stale_compose_temp_containers
  remove_conflicting_route_containers
  wait_for_route_container_names_gone
  if ! docker compose "${COMPOSE_FILES[@]}" up -d --build --force-recreate db; then
    collect_startup_diagnostics
    fail "Docker Compose database service failed to start"
  fi
  wait_for_db_healthy
  repair_db_roles
  if ! docker compose "${COMPOSE_FILES[@]}" up -d --build --no-recreate; then
    collect_startup_diagnostics
    fail "Docker Compose stack failed to start"
  fi
  wait_for_db_healthy
  repair_db_roles
  restart_db_client_services
}

certificate_issuer() {
  local domain="$1"
  timeout 12 openssl s_client -connect "${domain}:443" -servername "$domain" </dev/null 2>/dev/null |
    openssl x509 -noout -issuer 2>/dev/null || true
}

acme_rate_limit_message() {
  docker logs --since 20m traefik 2>&1 |
    grep -E 'urn:ietf:params:acme:error:rateLimited|too many certificates|too many new orders|retry after [0-9]{4}-[0-9]{2}-[0-9]{2}' |
    tail -n 1 || true
}

wait_for_letsencrypt() {
  if [ "$VERIFY_HTTPS" != "true" ]; then
    log "Skipping HTTPS certificate verification because VERIFY_HTTPS=${VERIFY_HTTPS}"
    return
  fi

  local deadline domain issuer all_ok rate_limit
  deadline=$((SECONDS + 360))
  while [ "$SECONDS" -lt "$deadline" ]; do
    rate_limit="$(acme_rate_limit_message)"
    if [ -n "$rate_limit" ]; then
      docker logs --tail 120 traefik || true
      fail "Let's Encrypt rate limit encountered. ${rate_limit}. Wait until the retry-after time before using LETSENCRYPT_PRODUCTION=true again. Leave LETSENCRYPT_PRODUCTION=false for test resets."
    fi

    all_ok=true
    for domain in "$API_DOMAIN" "$STUDIO_DOMAIN" "$AUTH_DOMAIN" "$SYNC_API_DOMAIN"; do
      issuer="$(certificate_issuer "$domain")"
      if ! grep -Eiq 'Let.s Encrypt|ISRG Root|R[0-9]+' <<<"$issuer"; then
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

  docker logs --tail 200 traefik || true
  fail "Traefik did not obtain Let's Encrypt certificates within 360 seconds. Check DNS, ports 80/443, and Traefik logs."
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
  cd /
  require_root
  log "Installing OS packages"
  install_packages
  log "Installing Docker"
  install_docker
  log "Checking out ${REPO_URL}#${REPO_BRANCH} into ${INSTALL_DIR}"
  checkout_repo
  reexec_if_repo_updated "$@"
  log "Configuring stack for ${BASE_DOMAIN}"
  configure_env
  configure_authelia
  verify_compose_domains
  verify_dns_points_here
  configure_firewall
  if [ "$START_STACK" = "true" ]; then
    log "Starting Supabase, Sync API, and Traefik"
    start_stack
    wait_for_letsencrypt
  else
    log "Skipping stack start because START_STACK=${START_STACK}"
  fi
  write_summary
}

main "$@"
