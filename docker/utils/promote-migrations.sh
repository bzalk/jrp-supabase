#!/usr/bin/env bash
set -euo pipefail

source_db_url="${SOURCE_DB_URL:-}"
target_db_url="${TARGET_DB_URL:-}"
source_container="${SOURCE_DB_CONTAINER:-}"
target_container="${TARGET_DB_CONTAINER:-}"
source_user="${SOURCE_DB_USER:-supabase_admin}"
target_user="${TARGET_DB_USER:-supabase_admin}"
source_database="${SOURCE_DB_NAME:-postgres}"
target_database="${TARGET_DB_NAME:-postgres}"
source_env="${SOURCE_ENV:-dev}"
target_env="${TARGET_ENV:-stage}"
ledger_schema="${MIGRATION_LEDGER_SCHEMA:-_migrations}"
ledger_table="${MIGRATION_LEDGER_TABLE:-promoted_schema_migrations}"
batch_label="${MIGRATION_BATCH_LABEL:-}"
dry_run="false"
limit=""
sync_storage_buckets="true"

usage() {
  cat <<'EOF'
Usage:
  promote-migrations.sh --source-db-url <url> --target-db-url <url> [options]
  promote-migrations.sh --source-container <container> --target-db-url <url> [options]

Source options:
  --source-db-url <url>          Source Postgres connection string.
  --source-container <name>      Source Docker container with psql installed.
  --source-user <user>           Source container DB user. Default: supabase_admin
  --source-db-name <database>    Source container DB name. Default: postgres
  --source-env <name>            Source environment label. Default: dev

Target options:
  --target-db-url <url>          Target Postgres connection string.
  --target-container <name>      Target Docker container with psql installed.
  --target-user <user>           Target container DB user. Default: supabase_admin
  --target-db-name <database>    Target container DB name. Default: postgres
  --target-env <name>            Target environment label. Default: stage

Ledger options:
  --schema <schema>              Target promotion ledger schema. Default: _migrations
  --table <table>                Target promotion ledger table. Default: promoted_schema_migrations
  --batch-label <label>          Optional label stored with promoted rows.

Execution options:
  --limit <n>                    Apply only the first n pending source migrations.
  --dry-run                      Compare source/target and print what would run without writes.
  --no-sync-storage-buckets       Do not sync rows from storage.buckets before SQL migrations.

Behavior:
  - Reads source migrations from supabase_migrations.schema_migrations.
  - Syncs source storage.buckets metadata to the target before SQL migrations by default.
  - Applies source statements to the target in version order.
  - Tracks promoted versions in the target promotion ledger.
  - Fails if a previously promoted source version has a different checksum.
  - Stops on the first failed migration and marks it failed in the target ledger.

Examples:
  ./docker/utils/promote-migrations.sh \
    --source-container supabase-db \
    --target-db-url "$STAGE_DB_URL" \
    --source-env dev \
    --target-env stage \
    --batch-label menu-2026-05-25

  ./docker/utils/promote-migrations.sh \
    --source-db-url "$DEV_DB_URL" \
    --target-db-url "$PROD_DB_URL" \
    --target-env prod \
    --dry-run
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-db-url)
      source_db_url="${2:-}"
      shift 2
      ;;
    --target-db-url)
      target_db_url="${2:-}"
      shift 2
      ;;
    --source-container)
      source_container="${2:-}"
      shift 2
      ;;
    --target-container)
      target_container="${2:-}"
      shift 2
      ;;
    --source-user)
      source_user="${2:-}"
      shift 2
      ;;
    --target-user)
      target_user="${2:-}"
      shift 2
      ;;
    --source-db-name)
      source_database="${2:-}"
      shift 2
      ;;
    --target-db-name)
      target_database="${2:-}"
      shift 2
      ;;
    --source-env)
      source_env="${2:-}"
      shift 2
      ;;
    --target-env)
      target_env="${2:-}"
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
    --limit)
      limit="${2:-}"
      shift 2
      ;;
    --dry-run)
      dry_run="true"
      shift
      ;;
    --no-sync-storage-buckets)
      sync_storage_buckets="false"
      shift
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

require_one_endpoint() {
  local label="$1"
  local db_url="$2"
  local container="$3"

  if [ -n "$db_url" ] && [ -n "$container" ]; then
    echo "Use either --${label}-db-url or --${label}-container, not both." >&2
    exit 2
  fi

  if [ -z "$db_url" ] && [ -z "$container" ]; then
    echo "Missing ${label} endpoint. Pass --${label}-db-url or --${label}-container." >&2
    exit 2
  fi
}

validate_identifier() {
  local label="$1"
  local value="$2"
  case "$value" in
    ''|*[!A-Za-z0-9_]*|[0-9]*)
      echo "Invalid $label: $value" >&2
      exit 2
      ;;
  esac
}

require_one_endpoint "source" "$source_db_url" "$source_container"
require_one_endpoint "target" "$target_db_url" "$target_container"
validate_identifier "ledger schema name" "$ledger_schema"
validate_identifier "ledger table name" "$ledger_table"

if [ -n "$limit" ]; then
  case "$limit" in
    ''|*[!0-9]*)
      echo "Invalid --limit value: $limit" >&2
      exit 2
      ;;
  esac
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required." >&2
  exit 2
fi

if ! command -v sha256sum >/dev/null 2>&1; then
  echo "sha256sum is required." >&2
  exit 2
fi

if ! command -v perl >/dev/null 2>&1; then
  echo "perl is required to normalize Supabase ledger SQL." >&2
  exit 2
fi

if [ -n "$source_db_url$target_db_url" ] && ! command -v docker >/dev/null 2>&1 && ! command -v psql >/dev/null 2>&1; then
  echo "psql or docker is required for URL-based database connections." >&2
  exit 2
fi

ledger_ref="\"$ledger_schema\".\"$ledger_table\""
source_label="$source_env"
target_label="$target_env"

sql_quote() {
  local value="$1"
  value="${value//\'/\'\'}"
  printf "'%s'" "$value"
}

checksum_file() {
  sha256sum "$1" | awk '{print $1}'
}

normalize_sql_file() {
  local input_file="$1"
  local output_file="$2"

  # Supabase's schema_migrations.statements can contain flattened SQL where
  # line comments no longer have line breaks. Strip comments before replay so
  # a flattened `-- comment CREATE TABLE ...` does not turn into a no-op.
  perl -0777 -pe '
    my $statement_start = qr{
      (?:
        CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|POLICY|(?:UNIQUE\s+)?INDEX|SCHEMA|EXTENSION|TRIGGER|FUNCTION|PROCEDURE|TYPE|VIEW|MATERIALIZED\s+VIEW|SEQUENCE)
        | ALTER\s+TABLE
        | DROP\s+(?:POLICY|TABLE|INDEX|SCHEMA|TRIGGER|FUNCTION|PROCEDURE|TYPE|VIEW|MATERIALIZED\s+VIEW|SEQUENCE)
        | GRANT\b
        | REVOKE\b
        | COMMENT\s+ON
        | TRUNCATE\b
        | INSERT\s+INTO
        | UPDATE\s+\S+\s+SET
        | DELETE\s+FROM
        | SELECT\s+
        | DO\s+\$\$
      )
    }ix;
    s{/\*.*?\*/}{}gs;
    s{(?:^|\s)--\s.*?\s(?=$statement_start)}{\n}gis;
    s{CREATE\s+POLICY\s+("[^"]+"|\S+)\s+ON\s+([^\s]+)}{DROP POLICY IF EXISTS $1 ON $2;\nCREATE POLICY $1 ON $2}gi;
    s/;\s*/;\n/g;
    s/^\s+//;
    s/\s+$/\n/;
  ' "$input_file" > "$output_file"
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

psql_url_scalar() {
  local url="$1"
  local sql="$2"
  if command -v psql >/dev/null 2>&1; then
    psql -X -v ON_ERROR_STOP=1 -A -t -q "$url" -c "$sql"
  else
    printf '%s\n' "$sql" | docker run --rm -i postgres:16 \
      psql -X -v ON_ERROR_STOP=1 -A -t -q "$url"
  fi
}

psql_url_exec() {
  local url="$1"
  local sql="$2"
  if command -v psql >/dev/null 2>&1; then
    psql -X -v ON_ERROR_STOP=1 -q "$url" -c "$sql"
  else
    printf '%s\n' "$sql" | docker run --rm -i postgres:16 \
      psql -X -v ON_ERROR_STOP=1 -q "$url"
  fi
}

psql_url_file() {
  local url="$1"
  local file="$2"
  if command -v psql >/dev/null 2>&1; then
    psql -X -v ON_ERROR_STOP=1 "$url" -f "$file"
  else
    docker run --rm -i postgres:16 \
      psql -X -v ON_ERROR_STOP=1 "$url" < "$file"
  fi
}

psql_container_scalar() {
  local container="$1"
  local user="$2"
  local database="$3"
  local sql="$4"
  printf '%s\n' "$sql" | docker exec -i "$container" \
    psql -U "$user" -d "$database" -X -v ON_ERROR_STOP=1 -A -t -q
}

psql_container_exec() {
  local container="$1"
  local user="$2"
  local database="$3"
  local sql="$4"
  printf '%s\n' "$sql" | docker exec -i "$container" \
    psql -U "$user" -d "$database" -X -v ON_ERROR_STOP=1 -q
}

psql_container_file() {
  local container="$1"
  local user="$2"
  local database="$3"
  local file="$4"
  docker exec -i "$container" \
    psql -U "$user" -d "$database" -X -v ON_ERROR_STOP=1 < "$file"
}

source_scalar() {
  local sql="$1"
  if [ -n "$source_db_url" ]; then
    psql_url_scalar "$source_db_url" "$sql"
  else
    psql_container_scalar "$source_container" "$source_user" "$source_database" "$sql"
  fi
}

target_scalar() {
  local sql="$1"
  if [ -n "$target_db_url" ]; then
    psql_url_scalar "$target_db_url" "$sql"
  else
    psql_container_scalar "$target_container" "$target_user" "$target_database" "$sql"
  fi
}

target_exec() {
  local sql="$1"
  if [ -n "$target_db_url" ]; then
    psql_url_exec "$target_db_url" "$sql"
  else
    psql_container_exec "$target_container" "$target_user" "$target_database" "$sql"
  fi
}

target_file() {
  local file="$1"
  if [ -n "$target_db_url" ]; then
    psql_url_file "$target_db_url" "$file"
  else
    psql_container_file "$target_container" "$target_user" "$target_database" "$file"
  fi
}

ensure_source_ledger() {
  local exists
  exists="$(
    source_scalar "
select to_regclass('supabase_migrations.schema_migrations') is not null;
"
  )"

  if [ "$exists" != "t" ]; then
    echo "Source database does not have supabase_migrations.schema_migrations." >&2
    exit 1
  fi
}

ensure_target_ledger() {
  target_exec "
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
  version text primary key,
  statements text[],
  name text
);
create schema if not exists \"$ledger_schema\";
create table if not exists $ledger_ref (
  id bigserial primary key,
  source_environment text not null,
  target_environment text not null,
  source_version text not null,
  source_name text,
  checksum_sha256 text not null,
  statement_count integer not null,
  status text not null check (status in ('running', 'promoted', 'failed')),
  batch_id text not null,
  batch_label text,
  started_at timestamptz not null default now(),
  promoted_at timestamptz,
  duration_ms integer,
  promoted_by text not null default current_user,
  error_message text,
  unique (source_environment, source_version)
);
create index if not exists ${ledger_table}_status_idx on $ledger_ref (status);
create index if not exists ${ledger_table}_batch_id_idx on $ledger_ref (batch_id);
create index if not exists ${ledger_table}_target_env_idx on $ledger_ref (target_environment);
create table if not exists \"$ledger_schema\".\"promoted_storage_buckets\" (
  id bigserial primary key,
  source_environment text not null,
  target_environment text not null,
  bucket_id text not null,
  bucket_name text not null,
  checksum_sha256 text not null,
  synthetic_version text not null,
  status text not null check (status in ('running', 'synced', 'failed')),
  batch_id text not null,
  batch_label text,
  started_at timestamptz not null default now(),
  synced_at timestamptz,
  duration_ms integer,
  synced_by text not null default current_user,
  error_message text,
  unique (source_environment, bucket_id, checksum_sha256)
);
create index if not exists promoted_storage_buckets_status_idx on \"$ledger_schema\".\"promoted_storage_buckets\" (status);
create index if not exists promoted_storage_buckets_batch_id_idx on \"$ledger_schema\".\"promoted_storage_buckets\" (batch_id);
"
}

record_target_native_migration() {
  local version="$1"
  local name="$2"
  local statements_json="$3"

  target_exec "
with source_migration as (
  select $(sql_quote "$statements_json")::jsonb as statements
)
insert into supabase_migrations.schema_migrations (
  version,
  statements,
  name
)
select
  $(sql_quote "$version"),
  array(select jsonb_array_elements_text(statements) from source_migration),
  $(sql_quote "$name")
on conflict (version) do update
set statements = excluded.statements,
    name = excluded.name;
"
}

target_ledger_exists() {
  [ "$(
    target_scalar "
select to_regclass($(sql_quote "$ledger_schema.$ledger_table")) is not null;
"
  )" = "t" ]
}

target_record() {
  local version="$1"
  target_scalar "
select status || '|' || checksum_sha256
from $ledger_ref
where source_environment = $(sql_quote "$source_label")
  and source_version = $(sql_quote "$version")
limit 1;
"
}

fetch_source_migrations_json() {
  source_scalar "
select coalesce(
  jsonb_agg(
    jsonb_build_object(
      'version', version,
      'name', name,
      'statements', statements
    )
    order by version
  ),
  '[]'::jsonb
)::text
from supabase_migrations.schema_migrations;
"
}

fetch_source_storage_buckets_json() {
  source_scalar "
select coalesce(
  jsonb_agg(
    jsonb_build_object(
      'id', id,
      'name', name,
      'owner', owner,
      'created_at', created_at,
      'updated_at', updated_at,
      'public', public,
      'avif_autodetection', avif_autodetection,
      'file_size_limit', file_size_limit,
      'allowed_mime_types', allowed_mime_types,
      'owner_id', owner_id,
      'type', type::text
    )
    order by id
  ),
  '[]'::jsonb
)::text
from storage.buckets;
"
}

target_storage_bucket_record() {
  local bucket_id="$1"
  target_scalar "
select status || '|' || checksum_sha256
from \"$ledger_schema\".\"promoted_storage_buckets\"
where source_environment = $(sql_quote "$source_label")
  and bucket_id = $(sql_quote "$bucket_id")
order by id desc
limit 1;
"
}

target_storage_bucket_exists() {
  local bucket_id="$1"
  [ "$(
    target_scalar "
select exists(select 1 from storage.buckets where id = $(sql_quote "$bucket_id"));
"
  )" = "t" ]
}

record_target_native_storage_bucket_migration() {
  local version="$1"
  local name="$2"
  local statement="$3"

  target_exec "
insert into supabase_migrations.schema_migrations (
  version,
  statements,
  name
) values (
  $(sql_quote "$version"),
  array[$(sql_quote "$statement")],
  $(sql_quote "$name")
)
on conflict (version) do update
set statements = excluded.statements,
    name = excluded.name;
"
}

storage_bucket_upsert_sql() {
  local bucket_json="$1"

  cat <<EOF
with bucket_data as (
  select $(sql_quote "$bucket_json")::jsonb as b
)
insert into storage.buckets (
  id,
  name,
  owner,
  created_at,
  updated_at,
  public,
  avif_autodetection,
  file_size_limit,
  allowed_mime_types,
  owner_id,
  type
)
select
  b->>'id',
  b->>'name',
  nullif(b->>'owner', '')::uuid,
  nullif(b->>'created_at', '')::timestamptz,
  nullif(b->>'updated_at', '')::timestamptz,
  nullif(b->>'public', '')::boolean,
  nullif(b->>'avif_autodetection', '')::boolean,
  nullif(b->>'file_size_limit', '')::bigint,
  case
    when jsonb_typeof(b->'allowed_mime_types') = 'array'
    then array(select jsonb_array_elements_text(b->'allowed_mime_types'))
    else null
  end,
  nullif(b->>'owner_id', ''),
  coalesce(nullif(b->>'type', ''), 'STANDARD')::storage.buckettype
from bucket_data
on conflict (id) do update
set name = excluded.name,
    owner = excluded.owner,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at,
    public = excluded.public,
    avif_autodetection = excluded.avif_autodetection,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types,
    owner_id = excluded.owner_id,
    type = excluded.type;
EOF
}

sync_source_storage_buckets() {
  local source_buckets_json_file="$tmp_dir/source-storage-buckets.json"
  local source_bucket_count

  if [ "$sync_storage_buckets" != "true" ]; then
    echo "Storage bucket sync: disabled"
    return
  fi

  fetch_source_storage_buckets_json > "$source_buckets_json_file"
  source_bucket_count="$(jq 'length' "$source_buckets_json_file")"

  if [ "$source_bucket_count" -eq 0 ]; then
    echo "Storage buckets: none found in source"
    return
  fi

  echo "Storage buckets found in source: $source_bucket_count"

  jq -c '.[]' "$source_buckets_json_file" | while IFS= read -r bucket_json; do
    bucket_id="$(jq -r '.id' <<<"$bucket_json")"
    bucket_name="$(jq -r '.name' <<<"$bucket_json")"
    bucket_file="$tmp_dir/storage-bucket-$bucket_id.json"
    printf '%s\n' "$bucket_json" > "$bucket_file"
    bucket_checksum="$(checksum_file "$bucket_file")"
    synthetic_version="storage_bucket_${bucket_id}_${bucket_checksum:0:12}"
    synthetic_name="sync_storage_bucket_${bucket_id}"
    upsert_sql="$(storage_bucket_upsert_sql "$bucket_json")"

    if [ "$dry_run" = "true" ]; then
      if target_storage_bucket_exists "$bucket_id"; then
        echo "BUCKET  CHECK $bucket_id exists on target"
      else
        echo "BUCKET  PENDING $bucket_id $bucket_name"
      fi
      continue
    fi

    bucket_record="$(target_storage_bucket_record "$bucket_id" || true)"
    if [ -n "$bucket_record" ]; then
      bucket_status="${bucket_record%%|*}"
      bucket_record_checksum="${bucket_record#*|}"
      if [ "$bucket_status" = "synced" ] && [ "$bucket_record_checksum" = "$bucket_checksum" ]; then
        target_exec "$upsert_sql"
        record_target_native_storage_bucket_migration "$synthetic_version" "$synthetic_name" "$upsert_sql"
        echo "BUCKET  SKIP $bucket_id $bucket_name"
        continue
      fi
    fi

    bucket_start_ms="$(date +%s%3N)"
    echo "BUCKET  SYNC $bucket_id $bucket_name"
    target_exec "
insert into \"$ledger_schema\".\"promoted_storage_buckets\" (
  source_environment,
  target_environment,
  bucket_id,
  bucket_name,
  checksum_sha256,
  synthetic_version,
  status,
  batch_id,
  batch_label
) values (
  $(sql_quote "$source_label"),
  $(sql_quote "$target_label"),
  $(sql_quote "$bucket_id"),
  $(sql_quote "$bucket_name"),
  $(sql_quote "$bucket_checksum"),
  $(sql_quote "$synthetic_version"),
  'running',
  $(sql_quote "$batch_id"),
  $(sql_quote "$batch_label")
)
on conflict (source_environment, bucket_id, checksum_sha256) do update
set status = 'running',
    target_environment = excluded.target_environment,
    batch_id = excluded.batch_id,
    batch_label = excluded.batch_label,
    started_at = now(),
    error_message = null;
"

    if target_exec "$upsert_sql"; then
      bucket_end_ms="$(date +%s%3N)"
      bucket_duration_ms="$((bucket_end_ms - bucket_start_ms))"
      record_target_native_storage_bucket_migration "$synthetic_version" "$synthetic_name" "$upsert_sql"
      target_exec "
update \"$ledger_schema\".\"promoted_storage_buckets\"
set status = 'synced',
    synced_at = now(),
    duration_ms = $bucket_duration_ms,
    error_message = null
where source_environment = $(sql_quote "$source_label")
  and bucket_id = $(sql_quote "$bucket_id")
  and checksum_sha256 = $(sql_quote "$bucket_checksum");
"
      echo "BUCKET  SYNCED $bucket_id (${bucket_duration_ms}ms)"
    else
      target_exec "
update \"$ledger_schema\".\"promoted_storage_buckets\"
set status = 'failed',
    error_message = 'psql failed while syncing this bucket; see runner output'
where source_environment = $(sql_quote "$source_label")
  and bucket_id = $(sql_quote "$bucket_id")
  and checksum_sha256 = $(sql_quote "$bucket_checksum");
"
      echo "BUCKET  FAILED $bucket_id" >&2
      exit 1
    fi
  done
}

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

echo "Source environment: $source_label"
if [ -n "$source_container" ]; then
  echo "Source: docker container $source_container"
else
  echo "Source: database URL"
fi
echo "Target environment: $target_label"
if [ -n "$target_container" ]; then
  echo "Target: docker container $target_container"
else
  echo "Target: database URL"
fi
echo "Promotion ledger: $ledger_schema.$ledger_table"
echo "Batch ID: $batch_id"
if [ -n "$batch_label" ]; then
  echo "Batch label: $batch_label"
fi
if [ -n "$limit" ]; then
  echo "Apply limit: $limit"
fi
echo ""

ensure_source_ledger

if [ "$dry_run" = "true" ]; then
  if target_ledger_exists; then
    target_ledger_available="true"
  else
    target_ledger_available="false"
    echo "Target promotion ledger does not exist yet. Dry run will treat all source migrations as pending."
    echo ""
  fi
else
  ensure_target_ledger
  target_ledger_available="true"
fi

sync_source_storage_buckets
echo ""

source_json_file="$tmp_dir/source-migrations.json"
fetch_source_migrations_json > "$source_json_file"

source_count="$(jq 'length' "$source_json_file")"
if [ "$source_count" -eq 0 ]; then
  echo "No source migrations found."
  exit 0
fi

pending_versions_file="$tmp_dir/pending-versions"
: > "$pending_versions_file"

jq -c '.[]' "$source_json_file" | while IFS= read -r migration; do
  version="$(jq -r '.version' <<<"$migration")"
  name="$(jq -r '.name // ""' <<<"$migration")"
  statement_count="$(jq '.statements | length' <<<"$migration")"
  statements_json="$(jq -c '.statements' <<<"$migration")"
  raw_sql_file="$tmp_dir/$version.raw.sql"
  sql_file="$tmp_dir/$version.sql"
  jq -r '.statements | join("\n\n")' <<<"$migration" > "$raw_sql_file"
  normalize_sql_file "$raw_sql_file" "$sql_file"
  checksum="$(checksum_file "$sql_file")"

  if [ "$target_ledger_available" = "true" ]; then
    record="$(target_record "$version")"
  else
    record=""
  fi

  if [ -z "$record" ]; then
    printf '%s\n' "$version" >> "$pending_versions_file"
    echo "PENDING $version $name ($statement_count statement(s))"
    continue
  fi

  status="${record%%|*}"
  promoted_checksum="${record#*|}"

  if [ "$status" = "failed" ]; then
    printf '%s\n' "$version" >> "$pending_versions_file"
    if [ "$promoted_checksum" = "$checksum" ]; then
      echo "RETRY   $version $name (previous attempt failed)"
    else
      echo "RETRY   $version $name (previous attempt failed; normalized SQL changed)"
    fi
    continue
  fi

  if [ "$promoted_checksum" != "$checksum" ]; then
    echo "CHECKSUM MISMATCH $version $name" >&2
    echo "  target ledger: $promoted_checksum" >&2
    echo "  source:        $checksum" >&2
    echo "Refusing to continue because a previously promoted migration changed." >&2
    exit 1
  fi

  case "$status" in
    promoted)
      if [ "$dry_run" != "true" ]; then
        record_target_native_migration "$version" "$name" "$statements_json"
      fi
      echo "SKIP    $version $name"
      ;;
    running)
      echo "BLOCKED $version has status '$status'. Inspect $ledger_schema.$ledger_table before retrying." >&2
      exit 1
      ;;
    *)
      echo "BLOCKED $version has unknown status '$status'." >&2
      exit 1
      ;;
  esac
done

pending_count="$(wc -l < "$pending_versions_file" | tr -d ' ')"
echo ""
echo "Source migrations: $source_count"
echo "Pending promotions: $pending_count"

if [ "$dry_run" = "true" ]; then
  echo "Dry run complete. No target writes were performed."
  exit 0
fi

if [ -n "$limit" ] && [ "$pending_count" -gt "$limit" ]; then
  limited_versions_file="$tmp_dir/pending-limited"
  sed -n "1,${limit}p" "$pending_versions_file" > "$limited_versions_file"
  mv "$limited_versions_file" "$pending_versions_file"
  pending_count="$limit"
  echo "Promoting first $limit pending migration(s)."
fi

while IFS= read -r version; do
  [ -n "$version" ] || continue

  migration="$(jq -c --arg version "$version" '.[] | select(.version == $version)' "$source_json_file")"
  name="$(jq -r '.name // ""' <<<"$migration")"
  statement_count="$(jq '.statements | length' <<<"$migration")"
  statements_json="$(jq -c '.statements' <<<"$migration")"
  sql_file="$tmp_dir/$version.sql"
  checksum="$(checksum_file "$sql_file")"
  file_size="$(wc -c < "$sql_file" | tr -d ' ')"
  start_ms="$(date +%s%3N)"

  echo ""
  echo "Promoting $version $name ($statement_count statement(s), ${file_size} bytes)"

  target_exec "
insert into $ledger_ref (
  source_environment,
  target_environment,
  source_version,
  source_name,
  checksum_sha256,
  statement_count,
  status,
  batch_id,
  batch_label
) values (
  $(sql_quote "$source_label"),
  $(sql_quote "$target_label"),
  $(sql_quote "$version"),
  $(sql_quote "$name"),
  $(sql_quote "$checksum"),
  $statement_count,
  'running',
  $(sql_quote "$batch_id"),
  $(sql_quote "$batch_label")
)
on conflict (source_environment, source_version) do update
set target_environment = excluded.target_environment,
    source_name = excluded.source_name,
    checksum_sha256 = excluded.checksum_sha256,
    statement_count = excluded.statement_count,
    status = 'running',
    batch_id = excluded.batch_id,
    batch_label = excluded.batch_label,
    started_at = now(),
    promoted_at = null,
    duration_ms = null,
    error_message = null;
"

  if target_file "$sql_file"; then
    end_ms="$(date +%s%3N)"
    duration_ms="$((end_ms - start_ms))"
    record_target_native_migration "$version" "$name" "$statements_json"
    target_exec "
update $ledger_ref
set status = 'promoted',
    promoted_at = now(),
    duration_ms = $duration_ms,
    error_message = null
where source_environment = $(sql_quote "$source_label")
  and source_version = $(sql_quote "$version");
"
    echo "Promoted $version (${duration_ms}ms)"
  else
    target_exec "
update $ledger_ref
set status = 'failed',
    error_message = 'psql failed while executing this migration; see runner output'
where source_environment = $(sql_quote "$source_label")
  and source_version = $(sql_quote "$version");
"
    echo "Failed $version" >&2
    exit 1
  fi
done < "$pending_versions_file"

echo ""
echo "Promotion batch complete."
