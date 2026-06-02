# Frontend API Guide: Supabase Import and Clone

This guide describes the current API surface for building the import/clone UX.

The current implementation supports:

- Connecting sync-api to a Supabase account token.
- Listing Supabase organizations and projects.
- Fetching project metadata and backup metadata.
- Creating a read-only import plan.

The current implementation does not yet execute the import. Execution will be added next through `POST /v1/imports/platform-to-local`.

## Important Architecture Rule

The tool must stay separate from the Supabase databases being tested or cloned.

sync-api stores its own control-plane state in its Docker `/data` volume:

- environment registry
- job state
- branch snapshot metadata
- logs

sync-api must not use the source Supabase database or the target local test database as its internal metadata store.

The frontend should treat every Supabase project/database as a source or target, never as the tool database.

## Sync API Auth

All protected sync-api endpoints require:

```http
Authorization: Bearer <SYNC_API_TOKEN>
```

Public endpoints:

```text
GET /health
GET /v1/jrp-supabase-slim.json
GET /v1/branching.md
```

## Supabase Account Auth

To list organizations/projects, sync-api needs a Supabase Management API access token.

The frontend can send it per request:

```http
X-Supabase-Access-Token: <SUPABASE_PAT>
```

Alternatively, the server can be configured with:

```text
SUPABASE_ACCESS_TOKEN=<SUPABASE_PAT>
```

Recommended UX:

1. Ask the user for a Supabase personal access token.
2. Keep it only in the current browser/session unless the product has a secure server-side secret vault.
3. Send it to sync-api in `X-Supabase-Access-Token` for account discovery calls.
4. Do not store it in any Supabase project database.

## Endpoint Summary

```text
GET  /v1/supabase/organizations
GET  /v1/supabase/projects
GET  /v1/supabase/projects?organization_id={id}
GET  /v1/supabase/projects/{ref}
GET  /v1/supabase/projects/{ref}/backups

POST /v1/imports/plan
```

## List Organizations

```http
GET /v1/supabase/organizations
Authorization: Bearer <SYNC_API_TOKEN>
X-Supabase-Access-Token: <SUPABASE_PAT>
```

Response shape:

```json
{
  "generated_at_ms": 1780000000000,
  "organizations": [
    {
      "id": "org_id",
      "name": "Organization Name",
      "slug": "organization-slug"
    }
  ]
}
```

The exact organization fields come from the Supabase Management API and may include additional keys.

## List Projects

```http
GET /v1/supabase/projects
Authorization: Bearer <SYNC_API_TOKEN>
X-Supabase-Access-Token: <SUPABASE_PAT>
```

Optional organization filter:

```http
GET /v1/supabase/projects?organization_id={id}
```

Response shape:

```json
{
  "generated_at_ms": 1780000000000,
  "projects": [
    {
      "id": "project-ref-or-id",
      "ref": "project-ref",
      "name": "Project Name",
      "region": "us-east-1",
      "organization_id": "org_id"
    }
  ]
}
```

The exact project fields come from the Supabase Management API and may include additional keys.

## Get Project

```http
GET /v1/supabase/projects/{ref}
Authorization: Bearer <SYNC_API_TOKEN>
X-Supabase-Access-Token: <SUPABASE_PAT>
```

Response shape:

```json
{
  "generated_at_ms": 1780000000000,
  "project": {
    "ref": "project-ref",
    "name": "Project Name",
    "region": "us-east-1",
    "organization_id": "org_id"
  }
}
```

## List Project Backups

```http
GET /v1/supabase/projects/{ref}/backups
Authorization: Bearer <SYNC_API_TOKEN>
X-Supabase-Access-Token: <SUPABASE_PAT>
```

Response shape:

```json
{
  "generated_at_ms": 1780000000000,
  "project_ref": "project-ref",
  "backups": [
    {
      "id": "backup-id",
      "status": "completed"
    }
  ]
}
```

The exact backup fields come from the Supabase Management API.

## Build Import Plan

```http
POST /v1/imports/plan
Authorization: Bearer <SYNC_API_TOKEN>
Content-Type: application/json
```

Minimal platform source by project ref:

```json
{
  "database_mode": "schema-only",
  "source": {
    "type": "platform",
    "project_ref": "abcdefghijklmnopqrst",
    "access_token": "<SUPABASE_PAT>"
  },
  "target": {
    "type": "local"
  }
}
```

Planning with database inspection requires a database URL:

```json
{
  "database_mode": "schema-and-data",
  "source": {
    "type": "platform",
    "project_ref": "abcdefghijklmnopqrst",
    "db_url": "postgres://postgres.<project-ref>:<password>@aws-0-region.pooler.supabase.com:5432/postgres",
    "access_token": "<SUPABASE_PAT>"
  },
  "target": {
    "type": "local",
    "container": "supabase-db",
    "db_name": "postgres"
  },
  "include_storage_bucket_metadata": true,
  "include_storage_objects": false,
  "include_edge_functions": true,
  "exact_rows": false,
  "largest_table_limit": 20
}
```

## Import Plan Request Fields

Top-level fields:

```text
database_mode                    schema-only | schema-and-data
include_storage_bucket_metadata  boolean, default true
include_storage_objects          boolean, default false
include_edge_functions           boolean, default true
include_auth_data                boolean, defaults true only for schema-and-data
exact_rows                       boolean, default false
include_columns                  boolean, default false
largest_table_limit              integer, default 20
access_token                     optional Supabase PAT applied to both sides
source                           required object
target                           optional object
```

Side fields for `source` or `target`:

```text
type                 platform | local
env                  display/environment label
project_ref          Supabase hosted project ref
project_id           alias for project_ref
db_url               Postgres connection URL
container            local Docker DB container, usually supabase-db
user                 database user for local container connection
reset_user           future import/reset user
db_name              database name, usually postgres
access_token         Supabase PAT for this side
api_base             Supabase Management API base, defaults https://api.supabase.com
functions_api_base   alias for api_base
```

## Import Plan Response Shape

```json
{
  "generated_at_ms": 1780000000000,
  "kind": "import_plan",
  "control_plane": {
    "state_store": "sync-api-data",
    "state_store_kind": "filesystem",
    "data_dir": "/data",
    "environment_registry": "/data/environments.json",
    "branch_snapshot_dir": "/data/branches",
    "uses_source_database_for_tool_state": false,
    "uses_target_database_for_tool_state": false,
    "notes": "sync-api control-plane state is stored in its own /data volume..."
  },
  "options": {
    "database_mode": "schema-only",
    "include_storage_bucket_metadata": true,
    "include_storage_objects": false,
    "include_edge_functions": true,
    "include_auth_data": false,
    "exact_rows": false,
    "include_columns": false,
    "largest_table_limit": 20
  },
  "source": {
    "side": {
      "role": "source",
      "type": "platform",
      "project_ref": "abcdefghijklmnopqrst",
      "domain": "https://abcdefghijklmnopqrst.supabase.co",
      "db_url": "postgres://postgres:***@host/postgres",
      "has_access_token": true
    },
    "project": {
      "available": true,
      "project_ref": "abcdefghijklmnopqrst",
      "metadata": {}
    },
    "database": {
      "available": true,
      "endpoint": "host/postgres",
      "overview": {
        "database_name": "postgres",
        "server_version": "16.1",
        "server_version_num": 160001,
        "database_size_bytes": 123456,
        "extensions": [],
        "has_storage_buckets": true,
        "has_supabase_migrations": true
      },
      "table_count": 12,
      "estimated_total_table_bytes": 123456,
      "largest_tables": [],
      "storage_bucket_count": 2,
      "storage_buckets": []
    }
  },
  "target": {
    "side": {},
    "project": {},
    "database": {}
  },
  "warnings": [
    {
      "code": "storage_objects_not_included",
      "message": "Storage bucket metadata can be planned from the database...",
      "severity": "info"
    }
  ],
  "feasibility": {
    "can_plan_import": true,
    "can_run_platform_to_local_now": true,
    "schema_only_supported": true,
    "schema_and_data_supported": false,
    "requires_storage_object_copy": false,
    "requires_supabase_cli_dump": true,
    "requires_tool_database": false
  },
  "next_recommended_endpoint": "POST /v1/imports/platform-to-local"
}
```

## Recommended UX Flow

1. Ask for sync-api URL and sync-api token.
2. Ask for Supabase PAT.
3. Call `GET /v1/supabase/organizations`.
4. Let user select an organization.
5. Call `GET /v1/supabase/projects?organization_id={id}`.
6. Let user select source Project A.
7. Ask for Project A database connection string/password if database inspection or data import is needed.
8. Ask for target type:
   - local VPS self-hosted instance
   - future hosted clone project
9. Call `POST /v1/imports/plan`.
10. Show warnings and feasibility.
11. Let user choose:
   - schema-only
   - schema-and-data
   - include storage object copy later
   - include edge functions
12. Future: start `POST /v1/imports/platform-to-local`.

## Information The UX Needs To Collect

Required now:

- sync-api base URL
- sync-api token
- Supabase personal access token
- source Supabase project selection

Required for database inspection/import:

- source database connection string
- source database password if not already embedded in the URL
- target local instance selection or confirmation that `supabase-db` is the target container

Required later for storage object copy:

- source Supabase S3 endpoint
- source Supabase S3 access key ID
- source Supabase S3 secret access key
- target self-hosted S3 endpoint
- target self-hosted S3 access key ID
- target self-hosted S3 secret access key
- selected buckets or all buckets

Required later for hosted clone creation:

- target Supabase organization
- new project name
- region
- database password for new hosted project
- compute/plan choices if exposed through the Management API

## Current Limitation

`POST /v1/imports/plan` is read-only. It returns feasibility and warnings but does not create a local instance, dump data, restore data, copy storage objects, or create a hosted clone.
