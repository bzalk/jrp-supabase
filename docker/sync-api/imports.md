# Frontend Guide: Import and Clone UX

This guide describes the import/clone API surface and the job progress contract a web UI should use.

It is served without auth at:

```text
GET /v1/imports.md
```

The OpenAPI definition is served without auth at:

```text
GET /v1/jrp-supabase-slim.json
```

## Architecture Rule

sync-api is the control plane. It stores its own state in its Docker `/data` volume, not in the source Supabase project and not in the local database being imported into.

The frontend should treat every Supabase database as either a source or a target. Do not use a project being cloned or tested as the tool metadata store.

## Auth

Protected sync-api endpoints require:

```http
Authorization: Bearer <SYNC_API_TOKEN>
```

Supabase account discovery uses a Supabase Management API personal access token. The frontend can send it per request:

```http
X-Supabase-Access-Token: <SUPABASE_PAT>
```

or sync-api can be configured with:

```text
SUPABASE_ACCESS_TOKEN=<SUPABASE_PAT>
```

## Discovery Endpoints

```text
GET  /v1/supabase/organizations
GET  /v1/supabase/projects
GET  /v1/supabase/projects?organization_id={id}
GET  /v1/supabase/projects/{ref}
GET  /v1/supabase/projects/{ref}/backups
```

Use these endpoints to let the user choose the source Supabase organization and project.

## Planning Endpoint

```text
POST /v1/imports/plan
```

This endpoint is read-only. It checks source/target connectivity, summarizes database size and largest tables, reports warnings, and tells the UI whether the current request can be executed.

Example request:

```json
{
  "database_mode": "schema-and-data",
  "source": {
    "type": "platform",
    "project_ref": "abcdefghijklmnopqrst",
    "db_url": "postgres://postgres.<project-ref>:<password>@aws-0-region.pooler.supabase.com:5432/postgres"
  },
  "target": {
    "type": "local",
    "container": "supabase-db",
    "db_name": "postgres"
  },
  "include_storage_bucket_metadata": true,
  "include_storage_objects": false,
  "include_edge_functions": true,
  "largest_table_limit": 20
}
```

Important target container name for this deployment:

```text
supabase-db
```

Do not use `supabase_db_local`.

## Start Platform-To-Local Import

```text
POST /v1/imports/platform-to-local
```

This starts a long-running job. Destructive imports require:

```json
{
  "confirm": "CONFIRM"
}
```

Example schema-and-data request:

```json
{
  "confirm": "CONFIRM",
  "database_mode": "schema-and-data",
  "source": {
    "type": "platform",
    "project_ref": "abcdefghijklmnopqrst",
    "db_url": "postgres://postgres.<project-ref>:<password>@aws-0-region.pooler.supabase.com:5432/postgres"
  },
  "target": {
    "type": "local",
    "container": "supabase-db",
    "db_name": "postgres",
    "user": "postgres"
  },
  "include_storage_bucket_metadata": true,
  "include_storage_objects": false,
  "include_edge_functions": false,
  "include_auth_data": true
}
```

Example dry run:

```json
{
  "dry_run": true,
  "database_mode": "schema-only",
  "source": {
    "type": "platform",
    "project_ref": "abcdefghijklmnopqrst",
    "db_url": "postgres://postgres.<project-ref>:<password>@aws-0-region.pooler.supabase.com:5432/postgres"
  },
  "target": {
    "type": "local",
    "container": "supabase-db"
  }
}
```

The response is `202 Accepted` with a job:

```json
{
  "job": {
    "id": "7d77f41f-8657-43d6-a6d6-890097c5148c",
    "kind": "import_platform_to_local",
    "environment": "platform-to-local",
    "status": "queued",
    "progress": {
      "phase": "queued",
      "percent": 0,
      "message": "Queued",
      "updated_at_ms": 1780000000000,
      "details": {}
    }
  }
}
```

## Job Progress Endpoint

Poll the existing job endpoint:

```text
GET /v1/jobs/{id}
```

New import jobs include:

```json
{
  "job": {
    "id": "7d77f41f-8657-43d6-a6d6-890097c5148c",
    "status": "running",
    "exit_code": null,
    "progress": {
      "phase": "database_import",
      "percent": 35,
      "message": "Importing database with pg_dump/pg_restore",
      "updated_at_ms": 1780000001000,
      "details": {
        "database_mode": "schema-and-data",
        "include_table_data": true,
        "include_edge_functions": false,
        "include_storage_objects": false,
        "source_database": {
          "available": true,
          "endpoint": "aws-0-region.pooler.supabase.com/postgres",
          "table_count": 12,
          "estimated_total_table_bytes": 123456,
          "storage_bucket_count": 2,
          "largest_tables": []
        },
        "target_database": {
          "available": true,
          "endpoint": "supabase-db/postgres",
          "table_count": 0,
          "estimated_total_table_bytes": 0,
          "storage_bucket_count": 0,
          "largest_tables": []
        },
        "progress_granularity": "phase",
        "table_progress_note": "This first implementation uses pg_dump/pg_restore, so progress is reported by phase. Per-table copy progress requires a future table-by-table restore workflow."
      }
    },
    "output": "Platform-to-local import started\n..."
  }
}
```

Poll every 1-2 seconds while `status` is `queued` or `running`. Stop polling when `status` is `succeeded` or `failed`.

Use `progress.percent` for the progress bar, `progress.message` as the current status line, and `output` for an expandable log panel.

## Progress Phases

```text
queued
running
planning
planned
dry_run
database_import
database_imported
database_skipped
edge_functions
edge_functions_imported
edge_functions_skipped
finalizing
succeeded
failed
```

The current database import uses `pg_dump` and `pg_restore`, so the API reports phase-level progress. It does not yet report exact table-by-table bytes copied. The planning response and progress details include `largest_tables` so the UI can show the likely heavy tables before the job starts.

## UX Recommendations

1. Let the user choose organization and source project.
2. Ask for the source database URL/password only when database inspection or import is needed.
3. Call `POST /v1/imports/plan`.
4. Show feasibility, warnings, database mode, estimated size, bucket count, and largest tables.
5. Require a deliberate confirmation before a destructive platform-to-local import.
6. Start `POST /v1/imports/platform-to-local`.
7. Navigate to a job detail screen or open a modal with progress, current phase, and logs.
8. Poll `GET /v1/jobs/{id}` until terminal status.
9. On success, show that the local target database has been replaced.
10. On failure, show `progress.message` and the tail of `output`.

## Current Limitations

- `schema-and-data` execution requires `include_auth_data: true`; selective auth exclusion is not implemented yet.
- Storage object bytes are not copied by this endpoint.
- `include_storage_objects: true` is rejected until the Storage/S3 copy workflow exists.
- Edge function copy is supported only when source/target edge function metadata is configured.
- Progress is phase-level, not exact table-by-table copy progress.
- Jobs are stored in process memory, so a sync-api restart clears job history.
