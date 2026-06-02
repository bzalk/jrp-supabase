#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Create or update a named Supabase instance and wait for health checks.

Usage:
  ./utils/deploy-instance.sh
  ./utils/deploy-instance.sh upwork
  ./utils/deploy-instance.sh --name upwork --base-domain jamrockdev.com
  ./utils/deploy-instance.sh --name upwork --site-url https://app.upwork.example
  ./utils/deploy-instance.sh --name upwork --db-password upwork-2026!

No-argument mode:
  - prompts for a stored Netlify API token if needed
  - prompts for the base domain and suffix
  - creates the required A records in Netlify DNS
  - waits until the new hostnames resolve before provisioning continues
  - expects the bare instance suffix, such as `test`, not `studio-test` or a full hostname

What it does:
  1. Prompts for missing install details when run interactively with no name
  2. Creates or reconciles Netlify A records for API, Studio, and Auth hostnames
  3. Waits until those DNS records resolve to this host across public resolvers
  4. Creates docker/instances/supabase-<slug>/ if it does not already exist
  5. Validates the instance compose config
  6. Starts the instance
  7. Waits for local Kong, Studio, and Authelia endpoints
  8. Waits for valid proxied HTTPS endpoints, including a trusted TLS certificate, through the active edge proxy
EOF
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
docker_dir=$(CDPATH= cd -- "${script_dir}/.." && pwd)
config_dir="${docker_dir}/supabase-instance-install"
legacy_config_dir="${docker_dir}/.codex"
netlify_config_file="${config_dir}/netlify.env"
legacy_netlify_config_file="${legacy_config_dir}/netlify.env"

name=""
base_domain=""
api_domain=""
studio_domain=""
auth_domain=""
site_url=""
db_password=""
admin_user=""
admin_email=""
admin_password=""
netlify_token=""
interactive_prompt=0
zone_dns_servers_csv=""
dns_propagation_wait_seconds=30
created_dns_records=0

declare -a LOCAL_IPV4S=()
declare -a LOCAL_IPV6S=()

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

  if [[ ! -f "${file}" ]]; then
    return 0
  fi

  sed -n "s/^${key}=//p" "$file" | head -n 1
}

host_uses_base_domain() {
  local host="$1"
  local expected_base_domain="$2"

  [[ "${host}" == *".${expected_base_domain}" ]]
}

assert_existing_instance_matches_request() {
  local env_file="$1"
  local existing_api_host existing_studio_host existing_auth_host

  existing_api_host=$(read_env_value "${env_file}" "API_DOMAIN")
  existing_studio_host=$(read_env_value "${env_file}" "STUDIO_DOMAIN")
  existing_auth_host=$(read_env_value "${env_file}" "AUTH_DOMAIN")

  if [[ -n "${api_domain}" && "${api_domain}" != "${existing_api_host}" ]]; then
    echo "Existing instance ${project_name} already uses API host ${existing_api_host}, not ${api_domain}." >&2
    return 1
  fi

  if [[ -n "${studio_domain}" && "${studio_domain}" != "${existing_studio_host}" ]]; then
    echo "Existing instance ${project_name} already uses Studio host ${existing_studio_host}, not ${studio_domain}." >&2
    return 1
  fi

  if [[ -n "${auth_domain}" && "${auth_domain}" != "${existing_auth_host}" ]]; then
    echo "Existing instance ${project_name} already uses Auth host ${existing_auth_host}, not ${auth_domain}." >&2
    return 1
  fi

  if [[ -n "${base_domain}" ]]; then
    if ! host_uses_base_domain "${existing_api_host}" "${base_domain}" \
      || ! host_uses_base_domain "${existing_studio_host}" "${base_domain}" \
      || ! host_uses_base_domain "${existing_auth_host}" "${base_domain}"; then
      echo "Existing instance ${project_name} does not belong to base domain ${base_domain}." >&2
      return 1
    fi
  fi
}

infer_base_domain() {
  local current_url current_host

  current_url=$(read_env_value "${docker_dir}/.env" "SUPABASE_PUBLIC_URL")
  current_host="${current_url#*://}"
  current_host="${current_host%%/*}"

  if [[ "${current_host}" == supabase.* ]]; then
    printf '%s' "${current_host#supabase.}"
    return 0
  fi

  echo "Could not infer --base-domain from SUPABASE_PUBLIC_URL=${current_url}" >&2
  return 1
}

require_commands() {
  local missing=0
  local command_name

  for command_name in curl jq docker ip getent openssl; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
      echo "Missing required command: ${command_name}" >&2
      missing=1
    fi
  done

  if (( missing != 0 )); then
    exit 1
  fi
}

collect_local_ips() {
  local iface_pattern='^(lo|docker.*|br-.*|veth.*|cni.*|virbr.*|flannel.*|tailscale.*|zt.*)$'

  mapfile -t LOCAL_IPV4S < <(
    ip -o -4 addr show up scope global \
      | awk -v iface_pattern="${iface_pattern}" '$2 !~ iface_pattern {split($4, parts, "/"); print parts[1]}' \
      | sort -u
  )
  mapfile -t LOCAL_IPV6S < <(
    ip -o -6 addr show up scope global \
      | awk -v iface_pattern="${iface_pattern}" '$2 !~ iface_pattern {split($4, parts, "/"); print parts[1]}' \
      | sort -u
  )
}

array_contains() {
  local needle="$1"
  shift
  local item

  for item in "$@"; do
    if [[ "${item}" == "${needle}" ]]; then
      return 0
    fi
  done

  return 1
}

load_netlify_token() {
  if [[ -z "${netlify_token}" ]]; then
    netlify_token=$(read_env_value "${netlify_config_file}" "NETLIFY_API_TOKEN")
  fi

  if [[ -z "${netlify_token}" && -f "${legacy_netlify_config_file}" ]]; then
    netlify_token=$(read_env_value "${legacy_netlify_config_file}" "NETLIFY_API_TOKEN")
  fi
}

save_netlify_token() {
  local token="$1"

  mkdir -p "${config_dir}"
  umask 077
  printf 'NETLIFY_API_TOKEN=%s\n' "${token}" > "${netlify_config_file}"
  chmod 600 "${netlify_config_file}"
}

prompt_input() {
  local prompt="$1"
  local default_value="${2:-}"
  local response

  if [[ -n "${default_value}" ]]; then
    read -r -p "${prompt} [${default_value}]: " response
    printf '%s' "${response:-${default_value}}"
    return 0
  fi

  read -r -p "${prompt}: " response
  printf '%s' "${response}"
}

prompt_secret() {
  local prompt="$1"
  local response

  read -r -s -p "${prompt}: " response
  printf '\n' >&2
  printf '%s' "${response}"
}

prompt_dns_failure_action() {
  if [[ ! -t 0 ]]; then
    echo "DNS preflight failed and no interactive terminal is available. Exiting." >&2
    return 1
  fi

  while true; do
    printf 'DNS preflight failed. Type "recheck" to try again or "exit" to stop: ' >&2
    read -r response
    case "${response}" in
      recheck|r|R)
        return 0
        ;;
      exit|e|E)
        return 1
        ;;
      *)
        echo "Please answer recheck or exit." >&2
        ;;
    esac
  done
}

is_valid_domain() {
  [[ "$1" =~ ^[A-Za-z0-9.-]+$ ]] && [[ "$1" == *.* ]]
}

is_valid_token_candidate() {
  [[ -n "$1" ]]
}

netlify_api() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local url="https://api.netlify.com/api/v1${path}"
  local response_file
  local status

  response_file=$(mktemp)
  if [[ -n "${body}" ]]; then
    status=$(
      curl -sS -o "${response_file}" -w '%{http_code}' \
        -X "${method}" \
        -H "Authorization: Bearer ${netlify_token}" \
        -H 'Content-Type: application/json' \
        --data "${body}" \
        "${url}" || true
    )
  else
    status=$(
      curl -sS -o "${response_file}" -w '%{http_code}' \
        -X "${method}" \
        -H "Authorization: Bearer ${netlify_token}" \
        "${url}" || true
    )
  fi

  if [[ -z "${status}" || ! "${status}" =~ ^2 ]]; then
    echo "Netlify API request failed: ${method} ${path} (HTTP ${status:-000})" >&2
    cat "${response_file}" >&2
    rm -f "${response_file}"
    return 1
  fi

  cat "${response_file}"
  rm -f "${response_file}"
}

get_netlify_zone_json() {
  local domain="$1"
  netlify_api GET "/dns_zones" | jq -ce --arg domain "${domain}" '.[] | select((.name // .domain) == $domain)' | head -n 1
}

ensure_slug_available() {
  local slug="$1"
  local project_name="supabase-${slug}"
  local candidate

  if [[ -e "${docker_dir}/instances/${project_name}" ]]; then
    echo "An instance already exists for suffix '${slug}'." >&2
    return 1
  fi

  if [[ -d "${docker_dir}/instances" ]]; then
    while read -r candidate; do
      if [[ "$(read_env_value "${candidate}" "INSTANCE_SLUG")" == "${slug}" ]]; then
        echo "An instance already exists for suffix '${slug}'." >&2
        return 1
      fi
    done < <(find "${docker_dir}/instances" -mindepth 2 -maxdepth 2 -type f -name '.env' | sort)
  fi
}

reconcile_netlify_a_records() {
  local zone_id="$1"
  local records_json="$2"
  shift 2
  local host
  local ip
  local payload
  local existing_record_ids
  local existing_ip
  local keep

  for host in "$@"; do
    mapfile -t existing_record_ids < <(
      jq -r --arg host "${host}" '.[] | select(.hostname == $host and .type == "A") | .id' <<<"${records_json}"
    )

    for record_id in "${existing_record_ids[@]}"; do
      existing_ip=$(jq -r --arg id "${record_id}" '.[] | select(.id == $id) | .value' <<<"${records_json}")
      keep=0
      for ip in "${LOCAL_IPV4S[@]}"; do
        if [[ "${existing_ip}" == "${ip}" ]]; then
          keep=1
          break
        fi
      done

      if [[ "${keep}" -eq 0 ]]; then
        netlify_api DELETE "/dns_zones/${zone_id}/dns_records/${record_id}" >/dev/null
        echo "Removed stale Netlify A record: ${host} -> ${existing_ip}"
      fi
    done
  done

  for host in "$@"; do
    for ip in "${LOCAL_IPV4S[@]}"; do
      if jq -e --arg host "${host}" --arg ip "${ip}" '.[] | select(.hostname == $host and .type == "A" and .value == $ip)' <<<"${records_json}" >/dev/null; then
        echo "Netlify A record already correct: ${host} -> ${ip}"
        continue
      fi

      payload=$(jq -cn --arg type "A" --arg hostname "${host}" --arg value "${ip}" '{type: $type, hostname: $hostname, value: $value, ttl: 60}')
      netlify_api POST "/dns_zones/${zone_id}/dns_records" "${payload}" >/dev/null
      echo "Created Netlify A record: ${host} -> ${ip}"
    done
  done
}

ensure_netlify_a_records_match_local_ips() {
  local zone_id="$1"
  shift
  local host
  local ip
  local records_json

  records_json=$(netlify_api GET "/dns_zones/${zone_id}/dns_records")
  reconcile_netlify_a_records "${zone_id}" "${records_json}" "$@"
}

resolve_with_dig() {
  local host="$1"
  local resolver="$2"

  dig +short A "${host}" @"${resolver}" 2>/dev/null | awk 'NF {print $1}' | sort -u
}

check_dns_target_against_resolver() {
  local host="$1"
  local resolver_label="$2"
  shift 2
  local -a expected_ipv4s=("$@")
  local -a resolved_ipv4s=()
  local ip

  if [[ "${resolver_label}" == "system" ]]; then
    mapfile -t resolved_ipv4s < <(getent ahostsv4 "${host}" | awk '{print $1}' | sort -u || true)
  else
    mapfile -t resolved_ipv4s < <(resolve_with_dig "${host}" "${resolver_label}")
  fi

  if [[ "${#resolved_ipv4s[@]}" -eq 0 ]]; then
    echo "  ${host} via ${resolver_label}: no A records yet" >&2
    return 1
  fi

  for ip in "${resolved_ipv4s[@]}"; do
    if ! array_contains "${ip}" "${expected_ipv4s[@]}"; then
      echo "  ${host} via ${resolver_label}: unexpected A ${ip}" >&2
      return 1
    fi
  done

  for ip in "${expected_ipv4s[@]}"; do
    if ! array_contains "${ip}" "${resolved_ipv4s[@]}"; then
      echo "  ${host} via ${resolver_label}: missing expected A ${ip}" >&2
      return 1
    fi
  done

  echo "  ${host} via ${resolver_label}: ${resolved_ipv4s[*]}"
}

ensure_dns_preflight() {
  local -a hosts=("$@")
  local -a resolvers=()
  local host
  local ok
  local resolver

  collect_local_ips

  if [[ "${#LOCAL_IPV4S[@]}" -eq 0 ]]; then
    echo "Could not determine any non-container IPv4 addresses for this host." >&2
    exit 1
  fi

  if command -v dig >/dev/null 2>&1 && [[ -n "${zone_dns_servers_csv}" ]]; then
    IFS=',' read -r -a resolvers <<<"${zone_dns_servers_csv}"
  fi
  resolvers+=("1.1.1.1" "8.8.8.8" "9.9.9.9" "system")

  while true; do
    ok=1

    echo "DNS preflight:"
    echo "  Local IPv4: ${LOCAL_IPV4S[*]}"

    for host in "${hosts[@]}"; do
      for resolver in "${resolvers[@]}"; do
        if ! check_dns_target_against_resolver "${host}" "${resolver}" "${LOCAL_IPV4S[@]}"; then
          ok=0
        fi
      done
    done

    if [[ "${ok}" -eq 1 ]]; then
      echo "DNS preflight passed."
      return 0
    fi

    if ! prompt_dns_failure_action; then
      echo "Aborted before provisioning because DNS is not ready." >&2
      exit 1
    fi
  done
}

wait_for_url() {
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

wait_for_https_url() {
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

run_instance_compose() {
  docker compose \
    --project-name "${project_name}" \
    --env-file "${instance_dir}/.env" \
    -f "${instance_dir}/docker-compose.yml" \
    -f "${instance_dir}/docker-compose.instance.yml" \
    "$@"
}

print_instance_startup_diagnostics() {
  local services=(analytics auth rest storage supavisor studio kong functions)
  local service

  echo "Recent compose status for ${project_name}:" >&2
  run_instance_compose ps --format '{{.Service}}\t{{.Status}}' >&2 || true

  for service in "${services[@]}"; do
    echo >&2
    echo "Recent logs for ${service}:" >&2
    run_instance_compose logs --tail 40 "${service}" >&2 || true
  done
}

instance_services_need_password_repair() {
  local ps_output logs

  ps_output=$(run_instance_compose ps --format '{{.Service}}\t{{.Status}}' 2>/dev/null || true)
  if [[ -z "${ps_output}" ]]; then
    return 1
  fi

  if ! awk -F'\t' '
    $1 ~ /^(analytics|auth|rest|storage|supavisor)$/ && $2 !~ /^Up/ { found=1 }
    END { exit found ? 0 : 1 }
  ' <<<"${ps_output}"; then
    return 1
  fi

  logs=$(run_instance_compose logs --tail 120 analytics auth rest storage supavisor 2>&1 || true)
  grep -Eq 'password authentication failed for user|invalid_password' <<<"${logs}"
}

instance_supabase_database_exists() {
  local has_database

  has_database=$(run_instance_compose exec -T db psql -U postgres -d postgres -At -c \
    "select 1 from pg_database where datname = '_supabase'" 2>/dev/null || true)

  [[ "${has_database}" == "1" ]]
}

instance_needs_bootstrap_repair() {
  if instance_bootstrap_roles_ready; then
    return 1
  fi

  ! instance_supabase_database_exists
}

bootstrap_instance_db() {
  echo "Detected incomplete database bootstrap for ${project_name}; running Supabase init scripts inside the db container." >&2

  run_instance_compose exec -T db sh <<'EOF'
set -eu

db=/docker-entrypoint-initdb.d
password="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -U postgres -d postgres -v pgpass="${password}" <<'EOSQL'
SELECT format(
  'CREATE ROLE supabase_admin WITH LOGIN SUPERUSER CREATEDB CREATEROLE REPLICATION BYPASSRLS PASSWORD %L',
  :'pgpass'
)
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_roles
  WHERE rolname = 'supabase_admin'
)
\gexec
EOSQL

for sql in "$db"/init-scripts/*.sql; do
  echo "bootstrap: running $sql" >&2
  psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -U postgres -d postgres -f "$sql"
done

psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -U postgres -d postgres -v pgpass="${password}" <<'EOSQL'
SELECT format('ALTER ROLE supabase_admin WITH PASSWORD %L', :'pgpass')
\gexec
EOSQL

export PGPASSWORD="${password}"

for sql in "$db"/migrations/*.sql; do
  echo "bootstrap: running $sql" >&2
  psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -U supabase_admin -d postgres -f "$sql"
done

postinit=/etc/postgresql.schema.sql
if [ -e "$postinit" ]; then
  echo "bootstrap: running $postinit" >&2
  psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -U supabase_admin -d postgres -f "$postinit"
fi

psql -v ON_ERROR_STOP=1 --no-password --no-psqlrc -U supabase_admin -d postgres \
  -c 'SELECT extensions.pg_stat_statements_reset(); SELECT pg_stat_reset();' || true
EOF
}

repair_instance_db_passwords() {
  local db_password
  db_password=$(read_env_value "${instance_dir}/.env" "POSTGRES_PASSWORD")

  if [[ -z "${db_password}" ]]; then
    echo "Cannot repair database passwords because POSTGRES_PASSWORD is missing from ${instance_dir}/.env." >&2
    return 1
  fi

  echo "Detected Postgres role password mismatch for ${project_name}; re-syncing database roles from ${instance_dir}/.env." >&2

  run_instance_compose exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 -v new_passwd="${db_password}" <<'EOF'
SELECT format('ALTER ROLE %I WITH PASSWORD %L', rolname, :'new_passwd')
FROM pg_roles
WHERE rolname IN (
  'anon',
  'authenticated',
  'authenticator',
  'dashboard_user',
  'pgbouncer',
  'postgres',
  'service_role',
  'supabase_admin',
  'supabase_auth_admin',
  'supabase_functions_admin',
  'supabase_replication_admin',
  'supabase_storage_admin'
)
\gexec
EOF
}

instance_bootstrap_roles_ready() {
  local role_count

  role_count=$(run_instance_compose exec -T db psql -U postgres -d postgres -At -c \
    "select count(*) from pg_roles where rolname in ('supabase_admin', 'supabase_auth_admin', 'supabase_storage_admin', 'authenticator')" 2>/dev/null || true)

  [[ "${role_count}" == "4" ]]
}

print_instance_bootstrap_error() {
  echo "The Postgres cluster for ${project_name} is missing one or more required Supabase roles." >&2
  echo "This instance was likely initialized before the Supabase bootstrap SQL ran completely." >&2
  echo "If ${project_name} is disposable, remove ${instance_dir}/volumes/db/data and run deploy again." >&2
  echo "If it contains data you need, repair or rebuild the database bootstrap before starting the rest of the stack." >&2
}

start_instance_with_recovery() {
  local repaired=0

  if ! "${instance_dir}/up.sh"; then
    echo "docker compose up reported a startup failure for ${project_name}; checking whether this is a recoverable Postgres password mismatch." >&2
  fi

  if instance_needs_bootstrap_repair; then
    bootstrap_instance_db
    repaired=1
  fi

  if instance_services_need_password_repair; then
    repair_instance_db_passwords
    repaired=1
  fi

  if (( repaired == 1 )); then
    echo "Retrying ${project_name} startup after database recovery." >&2
    "${instance_dir}/up.sh"
  fi

  if ! run_instance_compose ps --format '{{.Service}}\t{{.Status}}' >/dev/null 2>&1; then
    return 1
  fi

  if ! instance_bootstrap_roles_ready; then
    print_instance_bootstrap_error
    return 1
  fi

  return 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      name="${2:-}"
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
    --netlify-token)
      netlify_token="${2:-}"
      shift 2
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
      if [[ -z "${name}" ]]; then
        name="$1"
        shift
      else
        echo "Unexpected argument: $1" >&2
        usage >&2
        exit 1
      fi
      ;;
  esac
done

require_commands
load_netlify_token

if [[ -z "${name}" ]]; then
  if [[ ! -t 0 ]]; then
    echo "A name slug is required when not running interactively." >&2
    usage >&2
    exit 1
  fi

  interactive_prompt=1

  while [[ -z "${netlify_token}" ]]; do
    netlify_token=$(prompt_secret "What is your Netlify API key")
    if ! is_valid_token_candidate "${netlify_token}"; then
      echo "Netlify API key cannot be empty." >&2
      netlify_token=""
      continue
    fi
  done

  inferred_base_domain=""
  inferred_base_domain=$(infer_base_domain 2>/dev/null || true)
  while true; do
    base_domain=$(prompt_input "What is the domain you want to use (e.g. jamrockdev.com)" "${base_domain:-${inferred_base_domain}}")
    if is_valid_domain "${base_domain}"; then
      break
    fi
    echo "Please enter a valid domain name." >&2
  done

  while true; do
    name=$(prompt_input "What is the suffix you want to use (e.g. upwork)" "${name}")
    if ! validate_slug_input "${name}"; then
      continue
    fi
    name=$(sanitize_slug "${name}")
    if [[ -z "${name}" ]]; then
      echo "Please enter a suffix with at least one letter or number." >&2
      continue
    fi
    if ensure_slug_available "${name}"; then
      break
    fi
  done
fi

if [[ -z "${name}" ]]; then
  echo "A name slug is required." >&2
  usage >&2
  exit 1
fi

if ! validate_slug_input "${name}"; then
  exit 1
fi

slug=$(sanitize_slug "${name}")
if [[ -z "${slug}" ]]; then
  echo "The provided name does not contain any usable characters." >&2
  exit 1
fi

project_name="supabase-${slug}"
instance_dir="${docker_dir}/instances/${project_name}"
instance_exists=0
if [[ -d "${instance_dir}" ]]; then
  instance_exists=1
fi

if [[ "${instance_exists}" -eq 1 ]]; then
  if [[ "${interactive_prompt}" -eq 1 ]]; then
    echo "An instance already exists for suffix '${slug}'. Choose a different suffix." >&2
    exit 1
  fi

  env_file="${instance_dir}/.env"
  assert_existing_instance_matches_request "${env_file}"
  echo "Reusing existing instance directory: ${instance_dir}" >&2
  api_host=$(read_env_value "${env_file}" "API_DOMAIN")
  studio_host=$(read_env_value "${env_file}" "STUDIO_DOMAIN")
  auth_host=$(read_env_value "${env_file}" "AUTH_DOMAIN")
  studio_port=$(read_env_value "${env_file}" "STUDIO_PORT")
  kong_http_port=$(read_env_value "${env_file}" "KONG_HTTP_PORT")
  authelia_port=$(read_env_value "${env_file}" "AUTHELIA_PORT")
else
  if [[ -z "${base_domain}" ]]; then
    base_domain=$(infer_base_domain)
  fi

  api_host="${api_domain:-${project_name}.${base_domain}}"
  studio_host="${studio_domain:-studio-${slug}.${base_domain}}"
  auth_host="${auth_domain:-auth-${slug}.${base_domain}}"

  if [[ -z "${netlify_token}" ]]; then
    echo "A Netlify API token is required to create DNS records for a new instance." >&2
    exit 1
  fi

  while true; do
    zone_json=$(get_netlify_zone_json "${base_domain}") && break
    if [[ "${interactive_prompt}" -eq 1 ]]; then
      echo "Could not access the Netlify DNS zone for ${base_domain} with the current API token." >&2
      netlify_token=""
      while [[ -z "${netlify_token}" ]]; do
        netlify_token=$(prompt_secret "Enter a different Netlify API key")
        if ! is_valid_token_candidate "${netlify_token}"; then
          echo "Netlify API key cannot be empty." >&2
          netlify_token=""
        fi
      done
      continue
    fi

    echo "Could not find Netlify DNS zone for ${base_domain}. Make sure the domain is in Netlify DNS and the token has access." >&2
    exit 1
  done

  save_netlify_token "${netlify_token}"
  zone_id=$(jq -r '.id' <<<"${zone_json}")
  zone_dns_servers_csv=$(jq -r '[.dns_servers[]?] | join(",")' <<<"${zone_json}")
  collect_local_ips
  if [[ "${#LOCAL_IPV4S[@]}" -eq 0 ]]; then
    echo "Could not determine any non-container IPv4 addresses for this host." >&2
    exit 1
  fi
  ensure_netlify_a_records_match_local_ips "${zone_id}" "${api_host}" "${studio_host}" "${auth_host}"
  created_dns_records=1
fi

if [[ "${instance_exists}" -eq 1 && -n "${netlify_token}" ]]; then
  zone_json=$(get_netlify_zone_json "${base_domain:-$(infer_base_domain)}") || true
  if [[ -n "${zone_json}" ]]; then
    zone_id=$(jq -r '.id' <<<"${zone_json}")
    collect_local_ips
    if [[ "${#LOCAL_IPV4S[@]}" -gt 0 ]]; then
      ensure_netlify_a_records_match_local_ips "${zone_id}" "${api_host}" "${studio_host}" "${auth_host}"
      created_dns_records=1
    fi
  fi
fi

if [[ "${created_dns_records}" -eq 1 && "${dns_propagation_wait_seconds}" -gt 0 ]]; then
  echo "Waiting ${dns_propagation_wait_seconds}s for DNS propagation before preflight checks..."
  sleep "${dns_propagation_wait_seconds}"
fi

ensure_dns_preflight "${api_host}" "${studio_host}" "${auth_host}"

if [[ "${instance_exists}" -eq 0 ]]; then
  create_args=("${slug}")
  if [[ -n "${base_domain}" ]]; then
    create_args+=(--base-domain "${base_domain}")
  fi
  if [[ -n "${api_domain}" ]]; then
    create_args+=(--api-domain "${api_domain}")
  fi
  if [[ -n "${studio_domain}" ]]; then
    create_args+=(--studio-domain "${studio_domain}")
  fi
  if [[ -n "${auth_domain}" ]]; then
    create_args+=(--auth-domain "${auth_domain}")
  fi
  if [[ -n "${site_url}" ]]; then
    create_args+=(--site-url "${site_url}")
  fi
  if [[ -n "${db_password}" ]]; then
    create_args+=(--db-password "${db_password}")
  fi
  if [[ -n "${admin_user}" ]]; then
    create_args+=(--admin-user "${admin_user}")
  fi
  if [[ -n "${admin_email}" ]]; then
    create_args+=(--admin-email "${admin_email}")
  fi
  if [[ -n "${admin_password}" ]]; then
    create_args+=(--admin-password "${admin_password}")
  fi
  "${script_dir}/create-instance.sh" "${create_args[@]}"
fi

env_file="${instance_dir}/.env"
api_host=$(read_env_value "${env_file}" "API_DOMAIN")
studio_host=$(read_env_value "${env_file}" "STUDIO_DOMAIN")
auth_host=$(read_env_value "${env_file}" "AUTH_DOMAIN")
studio_port=$(read_env_value "${env_file}" "STUDIO_PORT")
kong_http_port=$(read_env_value "${env_file}" "KONG_HTTP_PORT")
authelia_port=$(read_env_value "${env_file}" "AUTHELIA_PORT")

"${instance_dir}/config.sh" >/dev/null
if ! start_instance_with_recovery; then
  print_instance_startup_diagnostics
  exit 1
fi

wait_for_url "Local API" "http://127.0.0.1:${kong_http_port}/auth/v1/health" 180 '^(200|401)$'
wait_for_url "Local Studio" "http://127.0.0.1:${studio_port}/api/platform/profile" 180 '^200$'
wait_for_url "Local Auth" "http://127.0.0.1:${authelia_port}/api/health" 180 '^200$'

edge_proxy=$(detect_edge_proxy)
case "${edge_proxy}" in
  traefik)
    wait_for_https_url "Proxied API" "https://${api_host}/auth/v1/health" 180 '^(200|401)$' --resolve "${api_host}:443:127.0.0.1"
    wait_for_https_url "Proxied Studio" "https://${studio_host}/" 180 '^302$' --resolve "${studio_host}:443:127.0.0.1"
    wait_for_https_url "Proxied Auth" "https://${auth_host}/" 180 '^200$' --resolve "${auth_host}:443:127.0.0.1"
    ;;
  caddy)
    "${script_dir}/reload-caddy.sh"
    wait_for_https_url "Proxied API" "https://${api_host}/auth/v1/health" 180 '^(200|401)$' --resolve "${api_host}:443:127.0.0.1"
    wait_for_https_url "Proxied Studio" "https://${studio_host}/" 180 '^(200|302)$' --resolve "${studio_host}:443:127.0.0.1"
    wait_for_https_url "Proxied Auth" "https://${auth_host}/" 180 '^200$' --resolve "${auth_host}:443:127.0.0.1"
    ;;
  *)
    echo "No known edge proxy detected; skipped HTTPS route checks." >&2
    ;;
esac

echo "Instance ${project_name} is deployed."
echo "  API:    https://${api_host}"
echo "  Studio: https://${studio_host}"
echo "  Auth:   https://${auth_host}"
