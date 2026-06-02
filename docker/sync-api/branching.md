# Sync API Branching

This document describes the Sync API local branching endpoints and the expected web UX integration. It is served without authentication at `GET /v1/branching.md`, like `GET /v1/jrp-supabase-slim.json`.

The branch management API snapshots and restores the local Supabase runtime. Branch operations are asynchronous background jobs: create, save, switch, and reset return `202 Accepted` with a `job` object. A web client should poll `GET /v1/jobs/{id}` until the job status is `succeeded` or `failed`, then refresh branch state from `GET /v1/branches`.

Branches are not separate running database instances. The local Supabase stack exposes one database runtime. The active branch is the snapshot name that the Sync API associates with that runtime; switching branches restores a saved snapshot into that same runtime.

Production promotion is intentionally branch-gated. Protected environments, such as `production` or `prod`, only allow `POST /v1/environments/{name}/migrations/up` when the active branch is `main`. Feature branches must be merged into `main` before they can be promoted to a protected target. `migrations/plan` remains available from feature branches so users can inspect pending work before merging.

## Authentication

Public endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Health check. |
| `GET` | `/v1/jrp-supabase-slim.json` | OpenAPI definition. |
| `GET` | `/v1/branching.md` | This branching guide. |

All branch and job endpoints require:

```http
Authorization: Bearer <SYNC_API_TOKEN>
```

## Branch Model

A branch is a named snapshot stored under the Sync API branch data directory. The default Docker deployment mounts this at `/data/branches` inside the `sync-api` container.

Branch names must match:

```text
^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$
```

Returned branch objects include:

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Branch name. |
| `mode` | `full` or `app-only` | Snapshot mode. |
| `created_at` | string | First snapshot timestamp. |
| `updated_at` | string | Last snapshot timestamp. |
| `active` | boolean | Whether this branch is currently restored into the local runtime. |
| `includes_database` | boolean | Always true for branch snapshots. |
| `includes_storage_files` | boolean | Whether storage files are included. |
| `schemas` | string[] | `["*"]` for full branches, or selected schemas for app-only branches. |
| `excluded_schemas` | string[] | Present on app-only branches to show managed platform schemas that are intentionally excluded. |
| `notes` | string | Human-readable branch notes. |
| `source_branch` | string | Branch this snapshot was created from, when known. |
| `reset_from` | string | Source branch used by the last reset, when applicable. |
| `merged_from` | string | Feature branch last merged into this branch, when applicable. |
| `merged_at` | string | Timestamp of the last merge into this branch, when applicable. |
| `dump_file` | string | `db.dump` for full branches, `public.dump` for app-only branches. |
| `dump_size_bytes` | integer or null | Current dump size. |
| `storage_size_bytes` | integer or null | Current storage archive size. |

## Branch Modes

### Full

Full branches capture the local database as a full custom-format `pg_dump`, including table data, and include storage files by default.

Use this mode when the user expects a complete local Supabase state snapshot, including platform-managed schemas and storage object files. During full snapshot or restore, the API stops configured Supabase service containers, performs the database and storage work, then restarts the containers that were stopped.

Typical create request:

```json
{
  "name": "checkout-redesign",
  "mode": "full",
  "include_storage_files": true,
  "notes": "Before checkout table changes"
}
```

To create a full schema-only branch, set `data_mode` to `schema-only`. This captures database objects without table rows and does not include storage files.

### App-Only

App-only branches capture schema definitions and table data for selected application schemas, defaulting to `public`. Platform-managed schemas such as `auth`, `storage`, `_realtime`, `supabase_functions`, `extensions`, `graphql`, `net`, `pgbouncer`, `supabase_migrations`, and `vault` are excluded.

Use this mode for faster feature work when the user is changing application tables, functions, policies, or data in selected schemas and does not need to snapshot all Supabase platform state.

Typical create request:

```json
{
  "name": "feature-dashboard",
  "mode": "app-only",
  "schemas": ["public", "app"],
  "include_storage_files": false,
  "notes": "Dashboard schema work"
}
```

To create an app-only branch with empty application tables, set `data_mode` to `schema-only` or `include_table_data` to `false`:

```json
{
  "name": "empty-cart",
  "mode": "app-only",
  "schemas": ["public"],
  "data_mode": "schema-only",
  "include_storage_files": false
}
```

When `activate` is true, the create job restores the schema-only snapshot immediately so the active local runtime is actually empty. After users add rows to that branch, normal autosave/save operations include those rows by default. Set `include_table_data: false` on an explicit save only when the user wants to overwrite the snapshot as schema-only again.

## Endpoint Definitions

### `GET /v1/branches/schemas`

Lists valid schemas from the current local branch database. A web UX should use this endpoint to populate the app-only schema picker instead of asking the user to type comma-separated schema names.

Response:

```json
{
  "generated_at_ms": 1780056000000,
  "database": {
    "role": "local",
    "environment": "current",
    "database_name": "postgres",
    "default_app_schemas": ["public"],
    "app_only_excluded_schemas": ["auth", "storage", "realtime", "_realtime", "supabase_functions", "extensions", "graphql", "graphql_public", "net", "pgbouncer", "supabase_migrations", "vault"],
    "schemas": [
      {
        "name": "public",
        "owner": "postgres",
        "table_count": 12,
        "total_bytes": 1048576,
        "selectable_for_app_only": true,
        "excluded_from_app_only": false
      },
      {
        "name": "auth",
        "owner": "supabase_auth_admin",
        "table_count": 16,
        "total_bytes": 2097152,
        "selectable_for_app_only": false,
        "excluded_from_app_only": true
      }
    ]
  }
}
```

Use `selectable_for_app_only` to disable or hide schemas that should not be used for app-only branches.

### `GET /v1/environments/{name}/schemas?role=source|target|both`

Lists schemas for registered environment database sides. This is useful when a UX needs to show what exists in production, staging, or the local source side before a reset/copy workflow.

`role` defaults to `both`.

Response:

```json
{
  "environment": "production",
  "generated_at_ms": 1780056000000,
  "databases": [
    {
      "role": "source",
      "environment": "dev",
      "database_name": "postgres",
      "default_app_schemas": ["public"],
      "app_only_excluded_schemas": ["auth", "storage", "realtime", "_realtime", "supabase_functions", "extensions", "graphql", "graphql_public", "net", "pgbouncer", "supabase_migrations", "vault"],
      "schemas": []
    },
    {
      "role": "target",
      "environment": "production",
      "database_name": "postgres",
      "default_app_schemas": ["public"],
      "app_only_excluded_schemas": ["auth", "storage", "realtime", "_realtime", "supabase_functions", "extensions", "graphql", "graphql_public", "net", "pgbouncer", "supabase_migrations", "vault"],
      "schemas": []
    }
  ]
}
```

### `GET /v1/branches`

Lists all branch snapshots and the active branch name.

Response:

```json
{
  "active_branch": "main",
  "branches": [
    {
      "name": "main",
      "mode": "full",
      "active": true,
      "includes_database": true,
      "includes_storage_files": true,
      "schemas": ["*"],
      "created_at": "2026-05-29T12:00:00+00:00",
      "updated_at": "2026-05-29T12:00:00+00:00",
      "notes": "Full local Supabase state snapshot",
      "dump_file": "db.dump",
      "dump_size_bytes": 123456,
      "storage_size_bytes": 7890
    }
  ]
}
```

### `POST /v1/branches`

Creates a branch snapshot from the current local Supabase runtime.

Request fields:

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `name` | string | yes | none | Must match the branch name pattern. |
| `mode` | `full` or `app-only` | yes | none | May also be expressed as `full: true` or `app_only: true`. |
| `data_mode` | `schema-and-data` or `schema-only` | no | `schema-and-data` | Controls whether the initial branch snapshot includes table rows. |
| `include_table_data` | boolean | no | true | Boolean equivalent to `data_mode`; `false` creates a schema-only snapshot. |
| `include_storage_files` | boolean | no | true for full, false for app-only | Controls whether `/supabase-storage` is archived. |
| `schemas` | string[] | no | `["public"]` for app-only | Full branches use `["*"]`. Populate this from `GET /v1/branches/schemas`. |
| `notes` | string | no | generated description | Displayed back in branch metadata. |
| `overwrite` | boolean | no | false | When true, replaces an existing snapshot with the same name. |
| `activate` | boolean | no | true | When true, marks the new branch as active after the snapshot succeeds. |
| `no_owner` | boolean | no | true | Passes `--no-owner` to dump/restore. |
| `no_privileges` | boolean | no | false | Passes `--no-privileges` to dump/restore. |

`include_storage_files` cannot be true when `include_table_data` is false, because storage files are data and their database metadata would not be included in a schema-only dump.

Response:

```json
{
  "job": {
    "id": "24a2d0df-614d-42c0-bf11-069c79aa7d58",
    "kind": "branch_create",
    "environment": "feature-dashboard",
    "status": "queued",
    "exit_code": null,
    "created_at_ms": 1780056000000,
    "started_at_ms": null,
    "finished_at_ms": null,
    "command": ["supabranch", "create", "feature-dashboard", "--app-only", "--no-storage", "--schemas", "public"],
    "output": ""
  }
}
```

By default, the API marks the new branch active after the snapshot succeeds. No restore is needed during create because the snapshot is taken from the already-running local database. Set `activate: false` only when creating a saved snapshot that should not become the current working branch.

If there is no active branch yet and the user creates a non-`main` branch, the API first saves the current runtime as `main`, then creates the requested branch with `source_branch: "main"`. This gives the UX an immediate switch-back target for the state the feature branch was created from.

### `GET /v1/branches/active`

Returns the active branch name and branch metadata if the active branch snapshot still exists.

Response:

```json
{
  "active_branch": "main",
  "branch": {
    "name": "main",
    "mode": "full",
    "active": true
  }
}
```

`branch` is `null` when an active branch name is recorded but the snapshot metadata is missing.

### `POST /v1/branches/save`

Saves the currently active local runtime state back into the active branch snapshot.

Request body is optional and uses the same override fields as branch save:

```json
{
  "include_storage_files": true,
  "notes": "Known good state after auth fix"
}
```

Returns a `branch_save` job. If there is no active branch, the endpoint returns `400`.

### `GET /v1/branches/{name}`

Returns one branch snapshot.

Response:

```json
{
  "branch": {
    "name": "feature-dashboard",
    "mode": "app-only",
    "active": false,
    "schemas": ["public", "app"]
  }
}
```

Returns `404` when the branch does not exist.

### `POST /v1/branches/{name}/save`

Saves the current local runtime state into the named branch snapshot. This is useful when a UI wants an explicit "save this branch" action from a branch detail page.

Request fields:

| Field | Type | Notes |
| --- | --- | --- |
| `include_storage_files` | boolean | Overrides the branch's current storage inclusion. |
| `schemas` | string[] | Only applies to app-only branches. Populate this from `GET /v1/branches/schemas`. |
| `notes` | string | Replaces branch notes. |
| `no_owner` | boolean | Defaults to true. |
| `no_privileges` | boolean | Defaults to false. |

Returns a `branch_save` job.

### `POST /v1/branches/{name}/switch`

Restores a branch snapshot into the active local Supabase runtime and marks it active.

Request body:

```json
{
  "autosave": true
}
```

When `autosave` is true, the API first snapshots the currently active branch before restoring the requested branch. If the requested branch is already active and `autosave` is true, the job succeeds without restoring.

Returns a `branch_switch` job.

### `POST /v1/branches/{name}/merge`

Merges a feature branch snapshot into `main`. This is the explicit release-acceptance step before promoting to protected production targets.

Request body:

```json
{
  "target_branch": "main",
  "autosave": true,
  "activate": true,
  "notes": "Merged feature-cart into main"
}
```

Request fields:

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `target_branch` | string | `main` | Currently only `main` is supported. |
| `autosave` | boolean | true | If `{name}` is active, save its current runtime state before merging. |
| `activate` | boolean | true | Switch the local runtime to merged `main` after the merge. |
| `notes` | string | generated merge note | Stored on the updated `main` metadata. |
| `no_owner` | boolean | true | Used when autosaving the active source branch. |
| `no_privileges` | boolean | false | Used when autosaving the active source branch. |

Returns a `branch_merge` job.

Merge behavior:

1. If `{name}` is active and `autosave` is true, the API snapshots the current runtime back into `{name}`.
2. The saved `{name}` snapshot, dump, storage archive, and migration ledger are copied into `main`.
3. `main` keeps its original `created_at`, gets a fresh `updated_at`, and records `merged_from` and `merged_at`.
4. If `activate` is true, the API makes `main` the active branch. If the runtime already matches the autosaved source branch, no restore is needed; otherwise the merged `main` snapshot is restored.

Source and target branch names must be different. The target branch must already exist.

### `POST /v1/branches/{name}/reset`

Replaces the target branch snapshot with another branch snapshot. This updates the saved snapshot only; it does not switch the local runtime unless the user later calls the switch endpoint.

Request body:

```json
{
  "from": "main"
}
```

`source_branch` is also accepted as an alias for `from`.

Returns a `branch_reset` job. Source and target branch names must be different.

### `DELETE /v1/branches/{name}`

Deletes one branch snapshot.

The active branch cannot be deleted. The endpoint returns `400` if `{name}` is currently active and `404` if the branch does not exist.

Response:

```json
{
  "branch": {
    "name": "old-feature",
    "mode": "app-only",
    "active": false
  }
}
```

### `GET /v1/jobs`

Lists in-memory jobs without the `output` field. Use this for an activity list.

### `GET /v1/jobs/{id}`

Returns a job with `output`. Use this for progress, logs, and final error display.

Job statuses:

| Status | Meaning |
| --- | --- |
| `queued` | The job has been accepted and the background thread has not started yet. |
| `running` | The operation is in progress. |
| `succeeded` | The operation finished successfully. |
| `failed` | The operation failed. Display `output` and any server error context. |

Jobs are stored in process memory, so a Sync API restart clears job history.

## Database Data and Production Data

Branch snapshots include database data:

| Branch mode | Data mode | Database contents | Storage files |
| --- | --- | --- | --- |
| `full` | `schema-and-data` | Full local database dump with schema and table data, plus the local migration ledger. | Included by default. |
| `full` | `schema-only` | Full local database dump with schema only and no table rows, plus the local migration ledger. | Not included. |
| `app-only` | `schema-and-data` | Selected application schemas with schema and table data, plus the local migration ledger. | Not included by default. |
| `app-only` | `schema-only` | Selected application schemas with schema only and no table rows, plus the local migration ledger. | Not included. |

The migration ledger matters for promotion: migration promotion reads `supabase_migrations.schema_migrations` from the active local runtime and applies pending versions to the configured target. Branch switching restores this ledger so promoting from `main` does not accidentally include migrations that only exist on a feature branch.

Protected environments add one more promotion rule: `migrations/up` can only run from active branch `main`. If the active branch is `feature-cart`, a protected production `up` request returns an error before queuing a job. The user must merge `feature-cart` into `main`, then promote from `main`.

The branch API snapshots the current local Supabase runtime. It does not directly branch a hosted production project. To test against production-like data, first use the environment reset/copy workflow to copy production into the local runtime, then create a branch from that local state.

Typical production-data workflow:

1. Register an environment where the production database is one side and the local database is the other side.
2. Inspect available schemas with `GET /v1/environments/{name}/schemas?role=both`.
3. Run the appropriate reset endpoint so production data is copied into local. For example, if production is configured as the source and local is configured as the target, call `POST /v1/environments/{name}/reset/destination` with `reset_database: true`.
4. After the reset job succeeds, call `POST /v1/branches` to snapshot the local runtime as a full or app-only branch.
5. Use later branch switches to return to that production-data baseline without pulling from production again.

Be deliberate with this flow. Reset endpoints use `pg_dump` and `pg_restore` and are destructive to the side being reset.

## Web UX Integration

### Primary Screen

A user-friendly branching UI should put branch state in the main workflow, not behind raw API controls:

1. On load, call `GET /v1/branches`.
2. Show the active branch prominently near the project/environment switcher.
3. Call `GET /v1/branches/schemas` before opening create/save dialogs that expose app-only schema selection.
4. Render branches in a table or compact list with name, active badge, mode, schemas, storage indicator, last updated time, notes, and size.
5. Disable branch mutation actions while a branch job is queued or running. The server also enforces one branch maintenance operation at a time.
6. Provide an activity drawer or inline progress panel backed by `GET /v1/jobs/{id}`.

### Create Branch Flow

Recommended controls:

| UI Control | API Field |
| --- | --- |
| Branch name input | `name` |
| Mode segmented control: Full / App-only | `mode` |
| Include storage toggle | `include_storage_files` |
| Schema picker populated from `GET /v1/branches/schemas`, visible for app-only | `schemas` |
| Notes textarea | `notes` |
| Overwrite existing snapshot checkbox, hidden until name conflict | `overwrite` |

Recommended behavior:

1. Validate the branch name pattern client-side before submit.
2. Default mode to app-only for faster feature work unless the user explicitly needs storage or platform state.
3. Default app-only schemas from `database.default_app_schemas`; this is usually `["public"]`.
4. Submit `POST /v1/branches` with `activate: true` unless the user chose a snapshot-only advanced option.
5. Poll the returned job.
6. On success, refresh `GET /v1/branches`.
7. Expect the new branch to be active when `activate` is true.
8. If this was the first branch and it was not named `main`, expect the branch list to contain both `main` and the new feature branch.

### Save Current Branch

Expose a clear "Save branch" action when an active branch exists.

Recommended behavior:

1. Call `POST /v1/branches/save` with optional notes.
2. Poll the returned job.
3. Show job output in a details panel while saving.
4. Refresh branch metadata when the job succeeds.

### Switch Branch

Switching mutates the running local Supabase state, so the UI should treat it as a deliberate action.

Recommended behavior:

1. User selects an inactive branch and clicks "Switch".
2. Show a confirmation that the local database, and possibly storage files, will be restored from the selected snapshot.
3. Keep `autosave` enabled by default so current work is saved before restore.
4. Submit `POST /v1/branches/{name}/switch` with `{ "autosave": true }`.
5. Poll the returned job and show output.
6. On success, refresh branch state and reload dependent app data that may have changed.

### Reset Branch From Another Branch

Reset is a snapshot management action, not a runtime switch.

Recommended behavior:

1. User opens a branch action menu and chooses "Reset from...".
2. UI shows a source branch picker excluding the target branch.
3. Confirm that the target snapshot will be replaced.
4. Submit `POST /v1/branches/{target}/reset` with `{ "from": "<source>" }`.
5. Poll the job and refresh branches on success.

### Merge Feature Branch Into Main

Merge is the release acceptance action. It should be more prominent than reset, but less casual than switch.

Recommended behavior:

1. Show a "Merge into main" action on non-`main` branches.
2. If the selected feature branch is active, keep `autosave` enabled so current local changes are included.
3. Default `activate` to true so the user lands on `main` immediately after the merge.
4. Submit `POST /v1/branches/{feature}/merge` with `{ "target_branch": "main", "autosave": true, "activate": true }`.
5. Poll the returned job and show output.
6. On success, refresh `GET /v1/branches`; `main` should be active and should show `merged_from: "<feature>"`.
7. Refresh `GET /v1/environments/{env}/migrations`; pending migrations should now represent what `main` can promote.

### Promote To Production

Protected production promotion should be driven from branch state and environment state together.

Recommended behavior:

1. Treat an environment as protected when its returned config has `protected: true`.
2. When the selected environment is protected and `active_branch !== "main"`, disable the "Up" action.
3. Show a direct message such as: "Production can only be promoted from main. Merge this branch into main first."
4. Keep "Plan" enabled so the user can inspect the active branch's pending migrations before merging.
5. After merge succeeds and `main` is active, enable "Up" for the protected environment.
6. On `up`, call `POST /v1/environments/{name}/migrations/up` and poll the job.

### Delete Branch

Recommended behavior:

1. Hide or disable delete for the active branch.
2. Confirm deletion for inactive branches.
3. Submit `DELETE /v1/branches/{name}`.
4. Refresh `GET /v1/branches`.

## Error Handling

Common branch errors:

| HTTP Status | Scenario | UX Recommendation |
| --- | --- | --- |
| `400` | Invalid name, ambiguous mode, missing active branch, active branch deletion, source equals target, protected environment promoted from a non-main branch | Show inline validation or confirmation-specific error text. |
| `401` | Missing or invalid bearer token | Prompt for reconnect/re-authentication. |
| `404` | Branch or job not found | Refresh branch/job state and remove stale UI items. |
| `500` | Dump, restore, Docker, or filesystem failure | Show job output, keep the user on the current screen, and allow retry after the cause is fixed. |

## Recommended Polling

Use a short poll interval while the job is active:

```js
async function waitForJob(jobId, fetchJson) {
  while (true) {
    const { job } = await fetchJson(`/v1/jobs/${jobId}`)
    if (job.status === 'succeeded' || job.status === 'failed') return job
    await new Promise((resolve) => setTimeout(resolve, 1500))
  }
}
```

Refresh `GET /v1/branches` after every successful create, save, switch, merge, reset, or delete operation.
