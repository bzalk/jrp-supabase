#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"

migrations_dir="${MIGRATIONS_DIR:-$repo_root/supabase/migrations}"
db_url="${SUPABASE_DB_URL:-${DATABASE_URL:-}}"
ledger_schema="${MIGRATION_LEDGER_SCHEMA:-_migrations}"
ledger_table="${MIGRATION_LEDGER_TABLE:-applied_sql_scripts}"
batch_label="${MIGRATION_BATCH_LABEL:-}"
dry_run="false"
limit=""

usage() {
  cat <<'EOF'
Usage:
  migrate-up.sh --db-url <postgres-url> [--dir <sql-dir>] [--batch-label <label>] [--limit <n>] [--dry-run]

Environment:
  SUPABASE_DB_URL             Postgres connection string. DATABASE_URL is also accepted.
  MIGRATIONS_DIR              Directory containing migration SQL files.
  MIGRATION_LEDGER_SCHEMA     Ledger schema name. Default: _migrations
  MIGRATION_LEDGER_TABLE      Ledger table name. Default: applied_sql_scripts
  MIGRATION_BATCH_LABEL       Optional label stored with this run.

Behavior:
  - Runs *.sql files in lexical order, excluding *.down.sql.
  - Executes each SQL file exactly as provided; no transaction wrapper is added.
  - Tracks applied scripts in the target database by relative path and SHA-256 checksum.
  - Fails if an already-applied script has changed.
  - Stops on the first failed migration and marks that script as failed.

Examples:
  SUPABASE_DB_URL='postgresql://postgres.<ref>:<password>@aws-0-us-east-1.pooler.supabase.com:6543/postgres?sslmode=require' \
    ./docker/utils/migrate-up.sh --dir ./migrations --batch-label dev-2026-05-25

  ./docker/utils/migrate-up.sh --db-url "$SUPABASE_DB_URL" --dir ./migrations --dry-run
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --db-url)
      db_url="${2:-}"
      shift 2
      ;;
    --dir)
      migrations_dir="${2:-}"
      shift 2
      ;;
    --schema)
      ledger_schema="${2:-}"
      shift 2
      ;;
    --table)
      ledger_table="${2:-}"
      shift 2
      ;;
    --batch-label)
      batch_label="${2:-}"
      shift 2
      ;;
  --dry-run)
      dry_run="true"
      shift
      ;;
    --limit)
      limit="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$db_url" ]; then
  echo "Missing database URL. Pass --db-url or set SUPABASE_DB_URL." >&2
  exit 2
fi

if [ ! -d "$migrations_dir" ]; then
  echo "Migration directory does not exist: $migrations_dir" >&2
  exit 2
fi

if [ -n "$limit" ]; then
  case "$limit" in
    ''|*[!0-9]*)
      echo "Invalid --limit value: $limit" >&2
      exit 2
      ;;
  esac
fi

case "$ledger_schema" in
  ''|*[!A-Za-z0-9_]*|[0-9]*)
    echo "Invalid ledger schema name: $ledger_schema" >&2
    exit 2
    ;;
esac

case "$ledger_table" in
  ''|*[!A-Za-z0-9_]*|[0-9]*)
    echo "Invalid ledger table name: $ledger_table" >&2
    exit 2
    ;;
esac

migrations_dir="$(CDPATH= cd -- "$migrations_dir" && pwd)"
ledger_ref="\"$ledger_schema\".\"$ledger_table\""

if command -v psql >/dev/null 2>&1; then
  psql_mode="local"
elif command -v docker >/dev/null 2>&1; then
  psql_mode="docker"
else
  echo "Neither psql nor docker is available. Install psql or Docker." >&2
  exit 2
fi

sql_quote() {
  local value="$1"
  value="${value//\'/\'\'}"
  printf "'%s'" "$value"
}

run_sql() {
  local sql="$1"
  if [ "$psql_mode" = "local" ]; then
    psql -X -v ON_ERROR_STOP=1 -q "$db_url" -c "$sql"
  else
    printf '%s\n' "$sql" | docker run --rm -i postgres:16 \
      psql -X -v ON_ERROR_STOP=1 -q "$db_url"
  fi
}

run_sql_scalar() {
  local sql="$1"
  if [ "$psql_mode" = "local" ]; then
    psql -X -v ON_ERROR_STOP=1 -A -t -q "$db_url" -c "$sql"
  else
    printf '%s\n' "$sql" | docker run --rm -i postgres:16 \
      psql -X -v ON_ERROR_STOP=1 -A -t -q "$db_url"
  fi
}

run_sql_file() {
  local file="$1"
  if [ "$psql_mode" = "local" ]; then
    psql -X -v ON_ERROR_STOP=1 "$db_url" -f "$file"
  else
    docker run --rm -i postgres:16 \
      psql -X -v ON_ERROR_STOP=1 "$db_url" < "$file"
  fi
}

checksum_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    echo "sha256sum or shasum is required." >&2
    exit 2
  fi
}

batch_id="$(
  if command -v uuidgen >/dev/null 2>&1; then
    uuidgen
  elif [ -r /proc/sys/kernel/random/uuid ]; then
    cat /proc/sys/kernel/random/uuid
  else
    date +%Y%m%d%H%M%S
  fi
)"

ensure_ledger() {
  run_sql "
create schema if not exists \"$ledger_schema\";
create table if not exists $ledger_ref (
  id bigserial primary key,
  script_path text not null unique,
  script_name text not null,
  checksum_sha256 text not null,
  file_size_bytes bigint not null,
  status text not null check (status in ('running', 'applied', 'failed')),
  batch_id text not null,
  batch_label text,
  started_at timestamptz not null default now(),
  applied_at timestamptz,
  duration_ms integer,
  applied_by text not null default current_user,
  error_message text
);
create index if not exists ${ledger_table}_status_idx on $ledger_ref (status);
create index if not exists ${ledger_table}_batch_id_idx on $ledger_ref (batch_id);
"
}

ledger_exists() {
  [ "$(
    run_sql_scalar "
select to_regclass($(sql_quote "$ledger_schema.$ledger_table")) is not null;
"
  )" = "t" ]
}

mapfile -d '' files < <(
  find "$migrations_dir" -type f -name '*.sql' ! -name '*.down.sql' -print0 | sort -z
)

if [ "${#files[@]}" -eq 0 ]; then
  echo "No migration SQL files found in $migrations_dir"
  exit 0
fi

echo "Migration directory: $migrations_dir"
echo "Ledger table: $ledger_schema.$ledger_table"
echo "Batch ID: $batch_id"
if [ -n "$batch_label" ]; then
  echo "Batch label: $batch_label"
fi
echo "psql mode: $psql_mode"
if [ -n "$limit" ]; then
  echo "Apply limit: $limit"
fi
echo ""

if [ "$dry_run" = "true" ]; then
  if ledger_exists; then
    ledger_available="true"
  else
    ledger_available="false"
    echo "Ledger table does not exist yet. Dry run will treat all scripts as pending."
    echo ""
  fi
else
  ensure_ledger
  ledger_available="true"
fi

pending=()
for file in "${files[@]}"; do
  rel_path="${file#"$migrations_dir"/}"
  checksum="$(checksum_file "$file")"
  if [ "$ledger_available" = "true" ]; then
    record="$(
      run_sql_scalar "
select status || '|' || checksum_sha256
from $ledger_ref
where script_path = $(sql_quote "$rel_path")
limit 1;
"
    )"
  else
    record=""
  fi

  if [ -z "$record" ]; then
    pending+=("$file")
    echo "PENDING $rel_path"
    continue
  fi

  status="${record%%|*}"
  applied_checksum="${record#*|}"

  if [ "$applied_checksum" != "$checksum" ]; then
    echo "CHECKSUM MISMATCH $rel_path" >&2
    echo "  ledger: $applied_checksum" >&2
    echo "  file:   $checksum" >&2
    echo "Refusing to continue because an already-tracked migration changed." >&2
    exit 1
  fi

  case "$status" in
    applied)
      echo "SKIP    $rel_path"
      ;;
    running|failed)
      echo "BLOCKED $rel_path has status '$status'. Inspect $ledger_schema.$ledger_table before retrying." >&2
      exit 1
      ;;
    *)
      echo "BLOCKED $rel_path has unknown status '$status'." >&2
      exit 1
      ;;
  esac
done

echo ""
echo "Pending migrations: ${#pending[@]}"

if [ "$dry_run" = "true" ]; then
  echo "Dry run complete. No migration files were executed."
  exit 0
fi

if [ -n "$limit" ] && [ "${#pending[@]}" -gt "$limit" ]; then
  pending=("${pending[@]:0:$limit}")
  echo "Applying first $limit pending migration(s)."
fi

for file in "${pending[@]}"; do
  rel_path="${file#"$migrations_dir"/}"
  script_name="$(basename "$file")"
  checksum="$(checksum_file "$file")"
  file_size="$(wc -c < "$file" | tr -d ' ')"
  start_ms="$(date +%s%3N)"

  echo ""
  echo "Applying $rel_path"

  run_sql "
insert into $ledger_ref (
  script_path,
  script_name,
  checksum_sha256,
  file_size_bytes,
  status,
  batch_id,
  batch_label
) values (
  $(sql_quote "$rel_path"),
  $(sql_quote "$script_name"),
  $(sql_quote "$checksum"),
  $file_size,
  'running',
  $(sql_quote "$batch_id"),
  $(sql_quote "$batch_label")
);
"

  if run_sql_file "$file"; then
    end_ms="$(date +%s%3N)"
    duration_ms="$((end_ms - start_ms))"
    run_sql "
update $ledger_ref
set status = 'applied',
    applied_at = now(),
    duration_ms = $duration_ms,
    error_message = null
where script_path = $(sql_quote "$rel_path");
"
    echo "Applied $rel_path (${duration_ms}ms)"
  else
    run_sql "
update $ledger_ref
set status = 'failed',
    error_message = 'psql failed while executing this script; see runner output'
where script_path = $(sql_quote "$rel_path");
"
    echo "Failed $rel_path" >&2
    exit 1
  fi
done

echo ""
echo "Migration batch complete."
