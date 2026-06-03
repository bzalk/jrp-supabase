# Supabase Import and Clone Plan

## Objective

Build a safe workflow for moving a hosted Supabase project into a new self-hosted VPS instance, then optionally using that local instance as the source for a new hosted Supabase clone.

Primary flows:

```text
Supabase.com Project A -> local self-hosted Project localA
Supabase.com Project A -> localA -> Supabase.com Project B
```

`localA` is not a branch of Project B. It is an intermediate copy that can be inspected, transformed, scrubbed, tested, and then used to seed a separate hosted project.

## Current API Surface We Can Reuse

- `POST /v1/environments/setup`
  Registers source and target database/project sides.

- `GET /v1/environments/{name}/identity`
  Identifies hosted/local projects and can use Supabase Management API metadata when a project ref and access token are configured.

- `GET /v1/environments/{name}/schemas`
  Lists valid schemas for source/target databases.

- `GET /v1/environments/{name}/stats`
  Compares table counts, row estimates or exact rows, and storage bucket metadata.

- `GET /v1/environments/{name}/metadata`
  Compares database functions, triggers, and edge functions.

- `GET /v1/environments/{name}/migrations`
  Lists source migrations and target promotion status.

- `POST /v1/environments/{name}/reset/destination`
  Existing low-level side-to-side database and edge function reset. This is useful, but should not become the main hosted-project import UX.

## Important Constraints

- Hosted Supabase to self-hosted restore should prefer `supabase db dump`, not raw `pg_dump`, because the Supabase CLI applies platform-specific filtering for internal schemas, reserved roles, and idempotent restore behavior.

- Database backups include schema/data/auth database rows, but not Storage API file objects. Storage object transfer must be separate.

- Storage object migration should use the Storage/S3 API, such as `rclone` S3-to-S3 copy. Directly copying files into `volumes/storage` is not correct.

- Bucket definitions can come from database restore or explicit `storage.buckets` sync, but object bytes are separate.

- Edge functions are separate from database restore. We already have partial Management API and local filesystem support for listing, reading, deploying, and pruning edge functions.

- Hosted project cloning through Supabase's native "Restore to a new project" is database-only and paid-plan/physical-backup oriented. It does not copy storage objects, edge functions, API keys, auth provider settings, realtime settings, or all project config.

## Missing First-Class Endpoints

Supabase account/project discovery:

```text
GET /v1/supabase/organizations
GET /v1/supabase/projects
GET /v1/supabase/projects/{ref}
GET /v1/supabase/projects/{ref}/backups
```

Import and clone planning/execution:

```text
POST /v1/imports/plan
POST /v1/imports/platform-to-local
POST /v1/imports/local-to-platform
POST /v1/imports/platform-to-platform
```

Long-running job polling should continue using:

```text
GET /v1/jobs
GET /v1/jobs/{id}
```

## Import Modes

The import endpoints should accept explicit options:

```json
{
  "database_mode": "schema-only",
  "include_storage_bucket_metadata": true,
  "include_storage_objects": false,
  "include_edge_functions": true,
  "include_auth_data": true
}
```

Supported `database_mode` values:

- `schema-only`
  Creates structure without table data. Useful for empty test databases and migration validation.

- `schema-and-data`
  Restores schema plus table data. Useful for realistic testing and project cloning.

Future optional modes:

- `selected-schemas`
- `selected-tables`
- `scrubbed-data`
- `sampled-data`

## Recommended Step 1

Implement planning only:

```text
POST /v1/imports/plan
```

Status: implemented as the first read-only planning slice. It does not mutate either side and does not store tool metadata in any source or target Supabase database.

Also implemented:

```text
GET /v1/supabase/organizations
GET /v1/supabase/projects
GET /v1/supabase/projects/{ref}
GET /v1/supabase/projects/{ref}/backups
```

These endpoints use the Supabase Management API token from `X-Supabase-Access-Token` or `SUPABASE_ACCESS_TOKEN`.

The plan response should include:

- Supabase account token validation status.
- Source project identity: ref, name, organization, region.
- Source database connectivity status.
- Source Postgres version.
- Self-hosted target connectivity status, if provided.
- Database size.
- Table count and largest tables.
- Row estimates, with optional exact row counts.
- Installed extensions and extension compatibility warnings.
- Storage bucket list and bucket metadata.
- Edge function list.
- Whether schema-only import is feasible.
- Whether schema-and-data import is feasible.
- Warnings for large databases, Postgres version mismatch, missing extensions, auth configuration, storage object separation, and project settings that cannot be cloned automatically.

This endpoint should not mutate either side.

## Recommended Step 2

Implement:

```text
POST /v1/imports/platform-to-local
```

Status: first implementation added. It starts a long-running import job, requires `confirm: "IMPORT PLATFORM TO LOCAL"` unless `dry_run` is true, imports database state with `pg_dump`/`pg_restore`, and keeps sync-api tool state outside both source and target Supabase databases.

Pending follow-up work:

- Replace or augment raw `pg_dump` with the safer hosted-platform `supabase db dump` flow.
- Add Storage/S3 object copy.
- Add richer verification report after restore.
- Add local instance provisioning when the target VPS stack does not already exist.

Execution outline:

1. Validate source Supabase project and DB connection.
2. Validate local VPS stack is running and target DB is reachable.
3. Generate roles/schema/data dumps using `supabase db dump`.
4. Restore roles and schema.
5. If `database_mode = schema-and-data`, restore data with `session_replication_role = replica`.
6. Restore/sync storage bucket metadata.
7. Optionally copy edge functions.
8. Optionally run storage object copy through S3/rclone.
9. Run verification checks.
10. Return job output and a structured import report.

## Recommended Step 3

Implement:

```text
POST /v1/imports/local-to-platform
```

Execution outline:

1. Select or create hosted Supabase Project B through Management API.
2. Wait for Project B readiness.
3. Register Project B as target environment.
4. Restore local database into Project B using the safer hosted restore path.
5. Deploy edge functions if requested.
6. Sync bucket metadata and optionally copy storage objects through S3.
7. Return a clone report.

## Recommended Step 4

Implement:

```text
POST /v1/imports/platform-to-platform
```

This can either:

- Use Supabase-native restore-to-new-project when available and appropriate.
- Or run through the controlled path:

```text
Project A -> local temp workspace -> Project B
```

The controlled path allows scrubbing, table selection, sampling, and validation.

## UX Notes

The web UX should guide users through a wizard:

1. Connect Supabase account.
2. Choose source project.
3. Choose target type: local VPS or new hosted project.
4. Run import plan.
5. Show warnings and estimated work.
6. Choose import mode.
7. Start import job.
8. Stream job output.
9. Show verification report.
10. Save resulting environment registration.

The UX should make storage object transfer visibly separate from database restore.

## Design Decision

Do not mutate the existing reset endpoints into the primary import/clone UX. Keep reset as a low-level side-to-side operation. Add dedicated import/clone endpoints with explicit options, validation, and reporting.
