#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Create an isolated self-hosted Supabase instance using a name slug.

Usage:
  ./utils/create-instance.sh upwork
  ./utils/create-instance.sh --name upwork --base-domain jamrockdev.com
  ./utils/create-instance.sh --name client-a --site-url https://app.client-a.com --up

What it does:
  - creates docker/instances/supabase-<slug>/
  - copies compose/config assets without copying live Postgres or storage data
  - generates fresh secrets for the new instance
  - generates a dedicated Authelia config and admin user for the instance
  - allocates the next free host ports automatically
  - writes helper scripts: up.sh, down.sh, config.sh
  - adds Traefik labels for API + Studio + Auth hostnames
  - writes a Caddy fragment for environments using shared Caddy
  - with --up, starts the instance and waits for valid HTTPS certificates and route checks through the active edge proxy

Defaults for slug "upwork":
  Project name:   supabase-upwork
  API domain:     supabase-upwork.<base-domain>
  Studio domain:  studio-upwork.<base-domain>
  Auth domain:    auth-upwork.<base-domain>

Options:
  --name <slug>         Instance slug. Positional form is also supported.
  --base-domain <name>  Base domain. Defaults from SUPABASE_PUBLIC_URL in docker/.env.
  --api-domain <name>   Override the API hostname.
  --studio-domain <name> Override the Studio hostname.
  --auth-domain <name>  Override the Authelia hostname.
  --site-url <url>      Override SITE_URL in the generated .env.
  --db-password <pw>    Set the Postgres password. Defaults to <slug>-<year>!.
  --admin-user <name>   Default Authelia admin username. Defaults to admin.
  --admin-email <addr>  Default Authelia admin email. Defaults to admin@<base-domain>.
  --admin-password <pw> Set the default Authelia admin password. Defaults to a generated secret.
  --output-root <path>  Defaults to docker/instances.
  --up                  Start the instance after generating it.
  -h, --help            Show this message.

Notes:
  - Each generated instance gets its own Authelia service and auth hostname.
  - Reverse proxy routing is expected to happen through Traefik on this host.
  - Pass the bare instance suffix only, such as `test`, not `studio-test` or a full hostname.
EOF
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
docker_dir=$(CDPATH= cd -- "${script_dir}/.." && pwd)

slug=""
base_domain=""
api_domain=""
studio_domain=""
auth_domain=""
site_url=""
db_password=""
admin_user="admin"
admin_email=""
admin_password=""
output_root="${docker_dir}/instances"
bring_up=0

sanitize_slug() {
  local value="$1"

  value=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
  value=$(printf '%s' "$value" | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-{2,}/-/g')
  printf '%s' "$value"
}

validate_slug_input() {
  local value="$1"

  if [[ -z "${value}" ]]; then
    return 0
  fi

  if [[ "${value}" == *://* || "${value}" == *.* ]]; then
    echo "Use the bare instance suffix, not a URL or hostname: ${value}" >&2
    return 1
  fi

  case "${value}" in
    supabase-*|studio-*|auth-*)
      echo "Use the bare instance suffix, not a prefixed hostname label: ${value}" >&2
      return 1
      ;;
  esac
}

read_env_value() {
  local file="$1"
  local key="$2"
  sed -n "s/^${key}=//p" "$file" | head -n 1
}

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"

  KEY="$key" VALUE="$value" perl -0pi -e '
    my $key = $ENV{KEY};
    my $value = $ENV{VALUE};
    my $pattern = qr/^\Q$key\E=.*$/m;
    if ($_ =~ $pattern) {
      s/$pattern/$key=$value/m;
    } else {
      $_ .= "\n" if $_ !~ /\n\z/;
      $_ .= "$key=$value\n";
    }
  ' "$file"
}

replace_literal() {
  local file="$1"
  local search="$2"
  local replacement="$3"

  SEARCH="$search" REPLACEMENT="$replacement" perl -0pi -e '
    my $search = $ENV{SEARCH};
    my $replacement = $ENV{REPLACEMENT};
    s/\Q$search\E/$replacement/g;
  ' "$file"
}

copy_dir_contents() {
  local src="$1"
  local dest="$2"

  mkdir -p "$dest"
  if [[ -d "$src" ]]; then
    cp -a "${src}/." "$dest/"
  fi
}

wait_for_http_code() {
  local label="$1"
  local url="$2"
  local timeout="$3"
  local code_pattern="$4"
  shift 4

  local deadline=$((SECONDS + timeout))
  local code="000"
  while (( SECONDS < deadline )); do
    code=$(curl -ksS -o /dev/null -w '%{http_code}' --connect-timeout 2 "$@" "$url" || true)
    if [[ "${code}" =~ ${code_pattern} ]]; then
      echo "${label} is ready (HTTP ${code})."
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for ${label}: ${url} (last HTTP ${code})" >&2
  return 1
}

wait_for_https_code() {
  local label="$1"
  local url="$2"
  local timeout="$3"
  local code_pattern="$4"
  shift 4

  local deadline=$((SECONDS + timeout))
  local code="000"
  while (( SECONDS < deadline )); do
    code=$(curl -fsS -o /dev/null -w '%{http_code}' --connect-timeout 2 "$@" "$url" || true)
    if [[ "${code}" =~ ${code_pattern} ]]; then
      echo "${label} is ready (HTTP ${code}, TLS verified)."
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for ${label}: ${url} (last HTTP ${code}, TLS not yet trusted or route not ready)" >&2
  return 1
}

detect_edge_proxy() {
  local containers
  containers=$(docker ps --format '{{.Names}}' 2>/dev/null || true)
  if grep -qx 'traefik-uds9-traefik-1' <<<"${containers}"; then
    echo "traefik"
    return 0
  fi
  if grep -qx 'supabase-caddy' <<<"${containers}"; then
    echo "caddy"
    return 0
  fi
  echo "none"
}

random_hex() {
  local bytes="$1"
  openssl rand -hex "${bytes}"
}

random_password() {
  openssl rand -hex 12
}

hash_password() {
  local password="$1"
  openssl passwd -6 "${password}"
}

default_db_password() {
  local instance_slug="$1"
  printf '%s-%s!\n' "${instance_slug}" "$(date -u +%Y)"
}

declare -A reserved_ports=()

reserve_port_if_set() {
  local port="${1:-}"
  if [[ -n "$port" && "$port" =~ ^[0-9]+$ ]]; then
    reserved_ports["$port"]=1
  fi
}

load_reserved_ports_from_env() {
  local env_file="$1"
  reserve_port_if_set "$(read_env_value "$env_file" "STUDIO_PORT")"
  reserve_port_if_set "$(read_env_value "$env_file" "KONG_HTTP_PORT")"
  reserve_port_if_set "$(read_env_value "$env_file" "KONG_HTTPS_PORT")"
  reserve_port_if_set "$(read_env_value "$env_file" "POSTGRES_PORT")"
  reserve_port_if_set "$(read_env_value "$env_file" "POOLER_PROXY_PORT_TRANSACTION")"
  reserve_port_if_set "$(read_env_value "$env_file" "AUTHELIA_PORT")"
}

if command -v ss >/dev/null 2>&1; then
  while read -r port; do
    reserve_port_if_set "$port"
  done < <(ss -ltnH 2>/dev/null | awk '{print $4}' | sed -E 's/.*:([0-9]+)$/\1/' | sort -u)
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      slug="${2:-}"
      shift 2
      ;;
    --base-domain)
      base_domain="${2:-}"
      shift 2
      ;;
    --api-domain)
      api_domain="${2:-}"
      shift 2
      ;;
    --studio-domain)
      studio_domain="${2:-}"
      shift 2
      ;;
    --auth-domain)
      auth_domain="${2:-}"
      shift 2
      ;;
    --site-url)
      site_url="${2:-}"
      shift 2
      ;;
    --db-password)
      db_password="${2:-}"
      shift 2
      ;;
    --admin-user)
      admin_user="${2:-}"
      shift 2
      ;;
    --admin-email)
      admin_email="${2:-}"
      shift 2
      ;;
    --admin-password)
      admin_password="${2:-}"
      shift 2
      ;;
    --output-root)
      output_root="${2:-}"
      shift 2
      ;;
    --up)
      bring_up=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -z "${slug}" ]]; then
        slug="$1"
        shift
      else
        echo "Unexpected argument: $1" >&2
        usage >&2
        exit 1
      fi
      ;;
  esac
done

if [[ -z "${slug}" ]]; then
  echo "A name slug is required." >&2
  usage >&2
  exit 1
fi

if ! validate_slug_input "${slug}"; then
  exit 1
fi

slug=$(sanitize_slug "$slug")
if [[ -z "${slug}" ]]; then
  echo "The provided name does not contain any usable characters." >&2
  exit 1
fi

if [[ -z "${base_domain}" ]]; then
  current_url=$(read_env_value "${docker_dir}/.env" "SUPABASE_PUBLIC_URL")
  current_host="${current_url#*://}"
  current_host="${current_host%%/*}"
  if [[ "${current_host}" == supabase.* ]]; then
    base_domain="${current_host#supabase.}"
  else
    echo "Could not infer --base-domain from SUPABASE_PUBLIC_URL=${current_url}" >&2
    exit 1
  fi
fi

project_name="supabase-${slug}"
api_domain="${api_domain:-${project_name}.${base_domain}}"
studio_domain="${studio_domain:-studio-${slug}.${base_domain}}"
auth_domain="${auth_domain:-auth-${slug}.${base_domain}}"
admin_email="${admin_email:-admin@${base_domain}}"
instance_dir="${output_root}/${project_name}"

if [[ -e "${instance_dir}" ]]; then
  echo "Refusing to overwrite existing instance directory: ${instance_dir}" >&2
  exit 1
fi

load_reserved_ports_from_env "${docker_dir}/.env"
if [[ -d "${output_root}" ]]; then
  while read -r env_file; do
    load_reserved_ports_from_env "$env_file"
  done < <(find "${output_root}" -mindepth 2 -maxdepth 2 -type f -name '.env' | sort)
fi

allocate_port() {
  local start="$1"
  local port="$start"
  while [[ -n "${reserved_ports[$port]:-}" ]]; do
    ((port++))
  done
  reserved_ports["$port"]=1
  printf '%s' "$port"
}

studio_port=$(allocate_port 3100)
kong_http_port=$(allocate_port 8100)
kong_https_port=$(allocate_port 8500)
db_port=$(allocate_port 5500)
pooler_port=$(allocate_port 6500)
authelia_port=$(allocate_port 9092)

mkdir -p "${instance_dir}"

cp "${docker_dir}/docker-compose.yml" "${instance_dir}/docker-compose.yml"
cp "${docker_dir}/.env.example" "${instance_dir}/.env.example"
cp "${docker_dir}/.env" "${instance_dir}/.env"

copy_dir_contents "${docker_dir}/volumes/api" "${instance_dir}/volumes/api"
copy_dir_contents "${docker_dir}/volumes/functions" "${instance_dir}/volumes/functions"
copy_dir_contents "${docker_dir}/volumes/logs" "${instance_dir}/volumes/logs"
copy_dir_contents "${docker_dir}/volumes/pooler" "${instance_dir}/volumes/pooler"
copy_dir_contents "${docker_dir}/volumes/snippets" "${instance_dir}/volumes/snippets"

mkdir -p "${instance_dir}/volumes/db"
cp "${docker_dir}/volumes/db/"*.sql "${instance_dir}/volumes/db/"
chmod 644 "${instance_dir}/volumes/db/"*.sql
mkdir -p "${instance_dir}/volumes/db/data"
mkdir -p "${instance_dir}/volumes/storage"
mkdir -p "${instance_dir}/volumes/authelia"

cat > "${instance_dir}/docker-compose.instance.yml" <<'EOF'
services:
  studio:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-studio
    depends_on:
      analytics:
        condition: service_started
    ports:
      - ${STUDIO_PORT}:3000
    labels: !override
      - traefik.enable=true
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}-studio.rule=Host(`${STUDIO_DOMAIN}`)
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}-studio.entrypoints=websecure
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}-studio.tls.certresolver=letsencrypt
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}-studio.middlewares=authelia-forwardauth-${INSTANCE_SLUG}@docker
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}-studio.service=${INSTANCE_CONTAINER_PREFIX}-studio
      - traefik.http.services.${INSTANCE_CONTAINER_PREFIX}-studio.loadbalancer.server.port=3000

  authelia:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-authelia
    ports: !override
      - 127.0.0.1:${AUTHELIA_PORT}:9091
    labels: !override
      - traefik.enable=true
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}-auth.rule=Host(`${AUTH_DOMAIN}`)
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}-auth.entrypoints=websecure
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}-auth.tls.certresolver=letsencrypt
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}-auth.service=${INSTANCE_CONTAINER_PREFIX}-auth
      - traefik.http.services.${INSTANCE_CONTAINER_PREFIX}-auth.loadbalancer.server.port=9091
      - traefik.http.middlewares.authelia-forwardauth-${INSTANCE_SLUG}.forwardauth.address=http://127.0.0.1:${AUTHELIA_PORT}/api/authz/forward-auth
      - traefik.http.middlewares.authelia-forwardauth-${INSTANCE_SLUG}.forwardauth.trustForwardHeader=true
      - traefik.http.middlewares.authelia-forwardauth-${INSTANCE_SLUG}.forwardauth.authResponseHeaders=Remote-User,Remote-Groups,Remote-Name,Remote-Email
    profiles: !reset []

  kong:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-kong
    labels: !override
      - traefik.enable=true
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}.rule=Host(`${API_DOMAIN}`)
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}.entrypoints=websecure
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}.tls.certresolver=letsencrypt
      - traefik.http.routers.${INSTANCE_CONTAINER_PREFIX}.service=${INSTANCE_CONTAINER_PREFIX}
      - traefik.http.services.${INSTANCE_CONTAINER_PREFIX}.loadbalancer.server.port=8000

  auth:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-auth

  rest:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-rest

  realtime:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-realtime

  storage:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-storage

  imgproxy:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-imgproxy

  meta:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-meta

  functions:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-functions

  analytics:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-analytics
    healthcheck:
      start_period: 60s

  db:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-db

  vector:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-vector

  supavisor:
    container_name: ${INSTANCE_CONTAINER_PREFIX}-pooler
EOF

cat > "${instance_dir}/up.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
script_dir=\$(CDPATH= cd -- "\$(dirname -- "\$0")" && pwd)
exec docker compose \
  --project-name "${project_name}" \
  --env-file "\${script_dir}/.env" \
  -f "\${script_dir}/docker-compose.yml" \
  -f "\${script_dir}/docker-compose.instance.yml" \
  up -d
EOF

cat > "${instance_dir}/down.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
script_dir=\$(CDPATH= cd -- "\$(dirname -- "\$0")" && pwd)
exec docker compose \
  --project-name "${project_name}" \
  --env-file "\${script_dir}/.env" \
  -f "\${script_dir}/docker-compose.yml" \
  -f "\${script_dir}/docker-compose.instance.yml" \
  down
EOF

cat > "${instance_dir}/config.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
script_dir=\$(CDPATH= cd -- "\$(dirname -- "\$0")" && pwd)
exec docker compose \
  --project-name "${project_name}" \
  --env-file "\${script_dir}/.env" \
  -f "\${script_dir}/docker-compose.yml" \
  -f "\${script_dir}/docker-compose.instance.yml" \
  config
EOF

chmod +x "${instance_dir}/up.sh" "${instance_dir}/down.sh" "${instance_dir}/config.sh"

set_env_value "${instance_dir}/.env" "INSTANCE_PROJECT_NAME" "${project_name}"
set_env_value "${instance_dir}/.env" "INSTANCE_CONTAINER_PREFIX" "${project_name}"
set_env_value "${instance_dir}/.env" "INSTANCE_SLUG" "${slug}"
set_env_value "${instance_dir}/.env" "API_DOMAIN" "${api_domain}"
set_env_value "${instance_dir}/.env" "STUDIO_DOMAIN" "${studio_domain}"
set_env_value "${instance_dir}/.env" "AUTH_DOMAIN" "${auth_domain}"
set_env_value "${instance_dir}/.env" "STUDIO_PORT" "${studio_port}"
set_env_value "${instance_dir}/.env" "AUTHELIA_PORT" "${authelia_port}"

set_env_value "${instance_dir}/.env" "SUPABASE_PUBLIC_URL" "https://${api_domain}"
set_env_value "${instance_dir}/.env" "API_EXTERNAL_URL" "https://${api_domain}"
set_env_value "${instance_dir}/.env" "PROXY_DOMAIN" "${api_domain}"
set_env_value "${instance_dir}/.env" "POSTGRES_PORT" "${db_port}"
set_env_value "${instance_dir}/.env" "POOLER_PROXY_PORT_TRANSACTION" "${pooler_port}"
set_env_value "${instance_dir}/.env" "KONG_HTTP_PORT" "${kong_http_port}"
set_env_value "${instance_dir}/.env" "KONG_HTTPS_PORT" "${kong_https_port}"
set_env_value "${instance_dir}/.env" "POOLER_TENANT_ID" "${project_name}"
set_env_value "${instance_dir}/.env" "STORAGE_TENANT_ID" "${project_name}"
set_env_value "${instance_dir}/.env" "STUDIO_DEFAULT_ORGANIZATION" "${project_name}"
set_env_value "${instance_dir}/.env" "STUDIO_DEFAULT_PROJECT" "${project_name}"
set_env_value "${instance_dir}/.env" "AUTHELIA_SESSION_SECRET" "$(random_hex 32)"
set_env_value "${instance_dir}/.env" "AUTHELIA_STORAGE_ENCRYPTION_KEY" "$(random_hex 32)"

if [[ -n "${site_url}" ]]; then
  set_env_value "${instance_dir}/.env" "SITE_URL" "${site_url}"
fi

(
  cd "${instance_dir}"
  sh "${docker_dir}/utils/generate-keys.sh" --update-env >/dev/null
  sh "${docker_dir}/utils/add-new-auth-keys.sh" --update-env >/dev/null
  rm -f .env.old
)

db_password="${db_password:-$(default_db_password "${slug}")}"
set_env_value "${instance_dir}/.env" "POSTGRES_PASSWORD" "${db_password}"

replace_literal "${instance_dir}/docker-compose.yml" "name: supabase" "name: ${project_name}"

admin_password="${admin_password:-$(random_password)}"
admin_password_hash=$(hash_password "${admin_password}")
session_cookie_name="authelia_session_$(printf '%s' "${slug}" | tr '-' '_')"

cat > "${instance_dir}/volumes/authelia/configuration.yml" <<EOF
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
  issuer: ${base_domain}

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
    - domain: ${auth_domain}
      policy: bypass
    - domain: ${studio_domain}
      subject:
        - group:studio-admins
      policy: one_factor

session:
  secret: change-me-session-secret
  name: ${session_cookie_name}
  same_site: lax
  expiration: 12h
  inactivity: 45m
  remember_me: 14d
  cookies:
    - domain: ${base_domain}
      authelia_url: https://${auth_domain}
      default_redirection_url: https://${studio_domain}

regulation:
  max_retries: 5
  find_time: 2m
  ban_time: 15m

storage:
  encryption_key: change-me-storage-encryption-key
  local:
    path: /config/db.sqlite3

notifier:
  filesystem:
    filename: /config/notification.txt
EOF

cat > "${instance_dir}/volumes/authelia/users_database.yml" <<EOF
users:
  ${admin_user}:
    displayname: "Instance Admin"
    password: "${admin_password_hash}"
    email: ${admin_email}
    groups:
      - studio-admins
EOF

: > "${instance_dir}/volumes/authelia/notification.txt"
cat > "${instance_dir}/authelia-admin.generated.txt" <<EOF
Auth URL: https://${auth_domain}
Username: ${admin_user}
Password: ${admin_password}
Email: ${admin_email}
EOF
chmod 600 "${instance_dir}/authelia-admin.generated.txt"

cat > "${instance_dir}/db-admin.generated.txt" <<EOF
Host: 127.0.0.1
Port: ${db_port}
Database: postgres
Username: postgres
Password: ${db_password}
Pooler Tenant: ${project_name}
Session Pooler URL: postgresql://postgres.${project_name}:${db_password}@127.0.0.1:${db_port}/postgres
EOF
chmod 600 "${instance_dir}/db-admin.generated.txt"

mkdir -p "${instance_dir}/proxy"
cat > "${instance_dir}/proxy/${project_name}.caddy" <<EOF
# ${project_name}
${api_domain} {
    reverse_proxy 127.0.0.1:${kong_http_port}
}

${studio_domain} {
    reverse_proxy 127.0.0.1:${studio_port}
}

${auth_domain} {
    reverse_proxy 127.0.0.1:${authelia_port}
}
EOF

cat > "${instance_dir}/README.generated.md" <<EOF
# ${project_name}

Generated by \`utils/create-instance.sh\`.

Domains:
- API: https://${api_domain}
- Studio: https://${studio_domain}
- Auth: https://${auth_domain}

Host ports:
- Studio: ${studio_port}
- Kong HTTP: ${kong_http_port}
- Kong HTTPS: ${kong_https_port}
- Postgres: ${db_port}
- Pooler: ${pooler_port}
- Authelia: ${authelia_port}

Lifecycle:
- Start: \`./up.sh\`
- Stop: \`./down.sh\`
- Render config: \`./config.sh\`

Credentials:
- Authelia admin: \`./authelia-admin.generated.txt\`
- Database admin: \`./db-admin.generated.txt\`

Proxy:
- Fragment: \`./proxy/${project_name}.caddy\`
- Install/reload: \`../../utils/deploy-instance.sh ${slug}\`
EOF

echo "Created ${instance_dir}"
echo "  Slug:   ${slug}"
echo "  API:    https://${api_domain}"
echo "  Studio: https://${studio_domain}"
echo "  Auth:   https://${auth_domain}"
echo "  Ports:  studio=${studio_port} kong=${kong_http_port}/${kong_https_port} db=${db_port} pooler=${pooler_port} auth=${authelia_port}"
echo "  Proxy:  ${instance_dir}/proxy/${project_name}.caddy"
echo "  Admin:  ${instance_dir}/authelia-admin.generated.txt"
echo "  DB:     ${instance_dir}/db-admin.generated.txt"

if [[ "${bring_up}" -eq 1 ]]; then
  "${instance_dir}/up.sh"
  wait_for_http_code "Local API" "http://127.0.0.1:${kong_http_port}/auth/v1/health" 180 '^(200|401)$'
  wait_for_http_code "Local Studio" "http://127.0.0.1:${studio_port}/api/platform/profile" 180 '^200$'
  wait_for_http_code "Local Auth" "http://127.0.0.1:${authelia_port}/api/health" 180 '^200$'

  edge_proxy=$(detect_edge_proxy)
  case "${edge_proxy}" in
    traefik)
      wait_for_https_code "Proxied API" "https://${api_domain}/auth/v1/health" 180 '^(200|401)$' --resolve "${api_domain}:443:127.0.0.1"
      wait_for_https_code "Proxied Studio" "https://${studio_domain}/" 180 '^302$' --resolve "${studio_domain}:443:127.0.0.1"
      wait_for_https_code "Proxied Auth" "https://${auth_domain}/" 180 '^200$' --resolve "${auth_domain}:443:127.0.0.1"
      ;;
    caddy)
      "${script_dir}/reload-caddy.sh"
      wait_for_https_code "Proxied API" "https://${api_domain}/auth/v1/health" 180 '^(200|401)$' --resolve "${api_domain}:443:127.0.0.1"
      wait_for_https_code "Proxied Studio" "https://${studio_domain}/" 180 '^(200|302)$' --resolve "${studio_domain}:443:127.0.0.1"
      wait_for_https_code "Proxied Auth" "https://${auth_domain}/" 180 '^200$' --resolve "${auth_domain}:443:127.0.0.1"
      ;;
    *)
      echo "No known edge proxy detected; skipped HTTPS route checks." >&2
      ;;
  esac
fi
