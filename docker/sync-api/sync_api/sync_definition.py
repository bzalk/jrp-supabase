from .settings import *


def _base_sync_definition_file():
    if OPENAPI_FILE.exists():
        return OPENAPI_FILE
    local = Path(__file__).resolve().parents[1] / "jrp-supabase-slim.json"
    if local.exists():
        return local
    raise FileNotFoundError(f"Sync API definition file not found: {OPENAPI_FILE}")

def add_sync_api_routes(definition):
    bearer_auth = [{"bearerAuth": []}]
    error_response = {
        "description": "Error",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"}
            }
        },
    }
    json_content = {
        "application/json": {
            "schema": {
                "type": "object",
            }
        }
    }
    paths = definition.setdefault("paths", {})
    schemas = definition.setdefault("components", {}).setdefault("schemas", {})
    parameters = definition["components"].setdefault("parameters", {})

    parameters.setdefault(
        "BranchName",
        {
            "name": "name",
            "in": "path",
            "required": True,
            "schema": {
                "type": "string",
                "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$",
            },
            "example": "feature-new-reports",
        },
    )
    schemas.update(
        {
            "Branch": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "name": {"type": "string"},
                    "mode": {"type": "string", "enum": ["full", "app-only"]},
                    "created_at": {"type": "string"},
                    "updated_at": {"type": "string"},
                    "includes_database": {"type": "boolean"},
                    "includes_table_data": {"type": "boolean"},
                    "data_mode": {
                        "type": "string",
                        "enum": ["schema-and-data", "schema-only"],
                    },
                    "includes_storage_files": {"type": "boolean"},
                    "schemas": {"type": "array", "items": {"type": "string"}},
                    "excluded_schemas": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                    "source_branch": {"type": "string"},
                    "reset_from": {"type": "string"},
                    "merged_from": {"type": "string"},
                    "merged_at": {"type": "string"},
                    "active": {"type": "boolean"},
                    "dump_file": {"type": "string"},
                    "migration_ledger_file": {"type": "string"},
                    "migration_count": {"type": "integer"},
                    "dump_size_bytes": {"type": ["integer", "null"]},
                    "storage_size_bytes": {"type": ["integer", "null"]},
                },
                "required": [
                    "name",
                    "mode",
                    "created_at",
                    "updated_at",
                    "includes_database",
                    "includes_storage_files",
                    "schemas",
                ],
            },
            "DatabaseSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "owner": {"type": ["string", "null"]},
                    "table_count": {"type": "integer"},
                    "total_bytes": {"type": "integer"},
                    "selectable_for_app_only": {"type": "boolean"},
                    "excluded_from_app_only": {"type": "boolean"},
                },
                "required": [
                    "name",
                    "table_count",
                    "total_bytes",
                    "selectable_for_app_only",
                    "excluded_from_app_only",
                ],
            },
            "DatabaseSchemas": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "environment": {"type": "string"},
                    "database_name": {"type": "string"},
                    "schemas": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/DatabaseSchema"},
                    },
                    "default_app_schemas": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "app_only_excluded_schemas": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "database_name",
                    "schemas",
                    "default_app_schemas",
                    "app_only_excluded_schemas",
                ],
            },
            "MigrationSummary": {
                "type": "object",
                "properties": {
                    "version": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                    "statement_count": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "promoted", "failed", "running"],
                    },
                    "source_environment": {"type": "string"},
                    "target_environment": {"type": "string"},
                    "batch_id": {"type": ["string", "null"]},
                    "batch_label": {"type": ["string", "null"]},
                    "started_at": {"type": ["string", "null"]},
                    "promoted_at": {"type": ["string", "null"]},
                    "duration_ms": {"type": ["integer", "null"]},
                    "error_message": {"type": ["string", "null"]},
                },
                "required": [
                    "version",
                    "statement_count",
                    "status",
                    "source_environment",
                    "target_environment",
                ],
            },
            "MigrationDetail": {
                "allOf": [
                    {"$ref": "#/components/schemas/MigrationSummary"},
                    {
                        "type": "object",
                        "properties": {
                            "statements": {
                                "type": "array",
                                "description": (
                                    "Normalized executable SQL statements split for display. "
                                    "Flattened Supabase ledger comments are stripped and replay "
                                    "guards such as DROP POLICY IF EXISTS are included."
                                ),
                                "items": {"type": "string"},
                            },
                            "sql": {
                                "type": "string",
                                "description": (
                                    "The normalized statements joined with blank lines for "
                                    "code-editor display."
                                ),
                            },
                        },
                        "required": ["statements", "sql"],
                    },
                ]
            },
            "BranchCreateRequest": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "example": "feature-new-reports"},
                    "mode": {"type": "string", "enum": ["full", "app-only"]},
                    "data_mode": {
                        "type": "string",
                        "enum": ["schema-and-data", "schema-only"],
                        "default": "schema-and-data",
                        "description": (
                            "schema-only creates an empty-data branch by dumping schema "
                            "without table rows. Later saves include table data by default "
                            "unless include_table_data is explicitly false."
                        ),
                    },
                    "include_table_data": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "When false, creates the branch snapshot with schema only and no "
                            "table rows. This is incompatible with include_storage_files true."
                        ),
                    },
                    "full": {"type": "boolean"},
                    "app_only": {"type": "boolean"},
                    "include_storage_files": {"type": "boolean"},
                    "schemas": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["public"],
                    },
                    "notes": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                    "activate": {"type": "boolean", "default": True},
                    "no_owner": {"type": "boolean", "default": True},
                    "no_privileges": {"type": "boolean", "default": False},
                },
                "required": ["name"],
                "examples": [
                    {
                        "name": "feature-new-reports",
                        "mode": "full",
                        "include_storage_files": True,
                    },
                    {
                        "name": "feature-dashboard",
                        "mode": "app-only",
                        "schemas": ["public"],
                    },
                    {
                        "name": "empty-cart",
                        "mode": "app-only",
                        "schemas": ["public"],
                        "data_mode": "schema-only",
                    },
                ],
            },
            "BranchSaveRequest": {
                "type": "object",
                "properties": {
                    "data_mode": {
                        "type": "string",
                        "enum": ["schema-and-data", "schema-only"],
                    },
                    "include_table_data": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Defaults true so data added to a schema-only branch is saved "
                            "normally. Set false only to overwrite the snapshot as schema-only."
                        ),
                    },
                    "include_storage_files": {"type": "boolean"},
                    "schemas": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                    "no_owner": {"type": "boolean", "default": True},
                    "no_privileges": {"type": "boolean", "default": False},
                },
            },
            "BranchSwitchRequest": {
                "type": "object",
                "properties": {
                    "autosave": {"type": "boolean", "default": True},
                },
            },
            "BranchResetRequest": {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "example": "main"},
                    "source_branch": {"type": "string"},
                },
            },
            "BranchMergeRequest": {
                "type": "object",
                "properties": {
                    "target_branch": {
                        "type": "string",
                        "default": "main",
                        "description": "Target branch to update. Currently only main is supported.",
                    },
                    "autosave": {
                        "type": "boolean",
                        "default": True,
                        "description": "When the source branch is active, save it before merging.",
                    },
                    "activate": {
                        "type": "boolean",
                        "default": True,
                        "description": "Switch the local runtime to the merged target branch after merge.",
                    },
                    "notes": {"type": "string"},
                    "no_owner": {"type": "boolean", "default": True},
                    "no_privileges": {"type": "boolean", "default": False},
                },
            },
            "ImportPlanRequest": {
                "type": "object",
                "properties": {
                    "database_mode": {
                        "type": "string",
                        "enum": ["schema-only", "schema-and-data"],
                        "default": "schema-only",
                    },
                    "include_storage_bucket_metadata": {
                        "type": "boolean",
                        "default": True,
                    },
                    "include_storage_objects": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, the plan warns that object bytes must be copied "
                            "through Storage/S3, not database restore."
                        ),
                    },
                    "include_edge_functions": {"type": "boolean", "default": True},
                    "include_auth_data": {"type": "boolean"},
                    "exact_rows": {"type": "boolean", "default": False},
                    "include_columns": {"type": "boolean", "default": False},
                    "largest_table_limit": {"type": "integer", "default": 20},
                    "access_token": {
                        "type": "string",
                        "description": "Optional Supabase Management API token applied to both sides.",
                    },
                    "source": {"$ref": "#/components/schemas/ImportSide"},
                    "target": {"$ref": "#/components/schemas/ImportSide"},
                },
                "required": ["source"],
            },
            "ImportSide": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "Logical side type, for example platform or local.",
                    },
                    "env": {"type": "string"},
                    "project_ref": {"type": "string"},
                    "project_id": {"type": "string"},
                    "db_url": {"type": "string"},
                    "container": {
                        "type": "string",
                        "description": (
                            "Local Docker database container. Canonical value is "
                            "supabase-db. Legacy supabase_db_local is normalized to "
                            "supabase-db by platform-to-local import planning/execution."
                        ),
                    },
                    "user": {"type": "string"},
                    "reset_user": {"type": "string"},
                    "db_name": {"type": "string"},
                    "access_token": {"type": "string"},
                    "api_base": {"type": "string"},
                    "functions_api_base": {"type": "string"},
                },
            },
            "ImportPlan": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "generated_at_ms": {"type": "integer"},
                    "kind": {"type": "string"},
                    "control_plane": {
                        "type": "object",
                        "properties": {
                            "state_store": {"type": "string"},
                            "state_store_kind": {"type": "string"},
                            "uses_source_database_for_tool_state": {"type": "boolean"},
                            "uses_target_database_for_tool_state": {"type": "boolean"},
                        },
                    },
                    "options": {"type": "object"},
                    "source": {"type": "object"},
                    "target": {"type": "object"},
                    "warnings": {"type": "array", "items": {"type": "object"}},
                    "feasibility": {"type": "object"},
                    "next_recommended_endpoint": {"type": "string"},
                },
                "required": [
                    "generated_at_ms",
                    "kind",
                    "control_plane",
                    "options",
                    "source",
                    "target",
                    "warnings",
                    "feasibility",
                ],
            },
            "PlatformToLocalImportRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ImportPlanRequest"},
                    {
                        "type": "object",
                        "properties": {
                            "confirm": {
                                "type": "string",
                                "description": (
                                    "Required unless dry_run is true. Must be exactly "
                                    "CONFIRM."
                                ),
                            },
                            "dry_run": {"type": "boolean", "default": False},
                            "reset_database": {"type": "boolean", "default": True},
                            "reset_edge_functions": {"type": "boolean"},
                            "prune_edge_functions": {"type": "boolean", "default": True},
                            "clear_branches": {
                                "type": "boolean",
                                "default": True,
                                "description": (
                                    "When reset_database is true, clear local branch "
                                    "snapshots after a successful platform-to-local import "
                                    "so stale branches from the previous local database are "
                                    "not shown."
                                ),
                            },
                            "create_main_branch": {
                                "type": "boolean",
                                "default": True,
                                "description": (
                                    "When reset_database is true, create an active main "
                                    "branch snapshot from the imported local database after "
                                    "stale branches are cleared."
                                ),
                            },
                            "drop_target_schemas": {"type": "boolean", "default": True},
                            "no_owner": {"type": "boolean", "default": True},
                            "no_privileges": {"type": "boolean", "default": True},
                            "schemas": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional schema filters passed to pg_dump/pg_restore.",
                            },
                        },
                    },
                ]
            },
            "JobProgress": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "phase": {
                        "type": "string",
                        "description": "Machine-readable progress phase for UI state.",
                    },
                    "percent": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Best-effort percentage for progress bars.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Human-readable status message.",
                    },
                    "updated_at_ms": {
                        "type": "integer",
                        "description": "Unix epoch timestamp in milliseconds.",
                    },
                    "details": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Job-specific metadata useful for detail panes, such as "
                            "database mode, largest tables, and copy limitations."
                        ),
                    },
                },
                "required": ["phase", "percent", "message", "updated_at_ms", "details"],
            },
        }
    )

    job_summary = schemas.get("JobSummary")
    if isinstance(job_summary, dict):
        job_properties = job_summary.setdefault("properties", {})
        kind_schema = job_properties.get("kind")
        if isinstance(kind_schema, dict) and isinstance(kind_schema.get("enum"), list):
            kind_schema["enum"] = sorted(
                set(kind_schema["enum"])
                | {
                    "branch_create",
                    "branch_delete",
                    "branch_merge",
                    "branch_reset",
                    "branch_save",
                    "branch_switch",
                    "import_platform_to_local",
                }
            )
        job_properties["progress"] = {"$ref": "#/components/schemas/JobProgress"}
        job_properties["progress_events"] = {
            "type": "array",
            "description": (
                "Server-recorded progress history. Use this when the UI needs to show "
                "phases that may have happened between polling intervals."
            ),
            "items": {"$ref": "#/components/schemas/JobProgress"},
        }

    paths.update(
        {
            "/v1/jrp-supabase-slim.json": {
                "get": {
                    "summary": "JRP Supabase Slim Sync API definition",
                    "security": [],
                    "responses": {
                        "200": {
                            "description": "Sync API definition for the JRP Supabase Slim control plane",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            },
                        }
                    },
                }
            },
            "/v1/imports/plan": {
                "post": {
                    "summary": "Plan a hosted/local Supabase import without mutating either side",
                    "description": (
                        "Inspects configured source and target project/database details "
                        "and returns feasibility, warnings, and a structured plan. sync-api "
                        "control-plane state remains separate from source/target Supabase databases."
                    ),
                    "security": bearer_auth,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ImportPlanRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Import plan",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ImportPlan"}
                                }
                            },
                        },
                        "400": error_response,
                        "401": error_response,
                    },
                }
            },
            "/v1/imports/platform-to-local": {
                "post": {
                    "summary": "Start a platform-to-local Supabase import job",
                    "description": (
                        "Starts a long-running job that imports a hosted Supabase database "
                        "into a local/self-hosted target. First implementation copies the "
                        "database through pg_dump/pg_restore and can copy Edge Functions "
                        "when configured. Storage bucket metadata can be copied, but "
                        "Storage object bytes, object rows, and internal upload/index tables "
                        "are not copied by this endpoint."
                    ),
                    "security": bearer_auth,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/PlatformToLocalImportRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "202": {"$ref": "#/components/responses/JobAccepted"},
                        "400": error_response,
                        "401": error_response,
                    },
                }
            },
            "/v1/imports.md": {
                "get": {
                    "summary": "Frontend import and clone API guide",
                    "security": [],
                    "responses": {
                        "200": {
                            "description": "Markdown guide for import/clone UX integration",
                            "content": {
                                "text/markdown": {
                                    "schema": {"type": "string"}
                                }
                            },
                        }
                    },
                }
            },
            "/v1/supabase/organizations": {
                "get": {
                    "summary": "List Supabase organizations for the supplied account token",
                    "description": (
                        "Send the Supabase Management API token in X-Supabase-Access-Token "
                        "or configure SUPABASE_ACCESS_TOKEN."
                    ),
                    "security": bearer_auth,
                    "responses": {"200": {"description": "Organizations", "content": json_content}},
                }
            },
            "/v1/supabase/projects": {
                "get": {
                    "summary": "List Supabase projects for the supplied account token",
                    "security": bearer_auth,
                    "parameters": [
                        {
                            "name": "organization_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "Projects", "content": json_content}},
                }
            },
            "/v1/supabase/projects/{ref}": {
                "get": {
                    "summary": "Get one Supabase project through the Management API",
                    "security": bearer_auth,
                    "parameters": [
                        {
                            "name": "ref",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "Project", "content": json_content}},
                }
            },
            "/v1/supabase/projects/{ref}/backups": {
                "get": {
                    "summary": "List backups for one Supabase project",
                    "security": bearer_auth,
                    "parameters": [
                        {
                            "name": "ref",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "Backups", "content": json_content}},
                }
            },
            "/v1/branches": {
                "get": {
                    "summary": "List local Supabase branches",
                    "security": bearer_auth,
                    "responses": {"200": {"description": "Branches", "content": json_content}},
                },
                "post": {
                    "summary": "Create a local Supabase branch snapshot",
                    "security": bearer_auth,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BranchCreateRequest"}
                            }
                        },
                    },
                    "responses": {
                        "202": {"$ref": "#/components/responses/JobAccepted"},
                        "400": error_response,
                        "401": error_response,
                    },
                },
            },
            "/v1/branching.md": {
                "get": {
                    "summary": "Branching guide",
                    "security": [],
                    "responses": {
                        "200": {
                            "description": "Markdown guide for local branching endpoints and UX integration",
                            "content": {
                                "text/markdown": {
                                    "schema": {"type": "string"}
                                }
                            },
                        }
                    },
                }
            },
            "/v1/branches/schemas": {
                "get": {
                    "summary": "List schemas in the current local branch database",
                    "security": bearer_auth,
                    "responses": {
                        "200": {
                            "description": "Current local database schemas",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "generated_at_ms": {"type": "integer"},
                                            "database": {
                                                "$ref": "#/components/schemas/DatabaseSchemas"
                                            },
                                        },
                                        "required": ["generated_at_ms", "database"],
                                    }
                                }
                            },
                        },
                        "401": error_response,
                    },
                }
            },
            "/v1/environments/{name}/schemas": {
                "get": {
                    "summary": "List schemas for a registered environment database side",
                    "security": bearer_auth,
                    "parameters": [
                        {"$ref": "#/components/parameters/EnvironmentName"},
                        {
                            "name": "role",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "string",
                                "enum": ["source", "target", "both"],
                                "default": "both",
                            },
                            "description": "Which configured database side to inspect.",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Environment database schemas",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "environment": {"type": "string"},
                                            "generated_at_ms": {"type": "integer"},
                                            "databases": {
                                                "type": "array",
                                                "items": {
                                                    "$ref": "#/components/schemas/DatabaseSchemas"
                                                },
                                            },
                                        },
                                        "required": [
                                            "environment",
                                            "generated_at_ms",
                                            "databases",
                                        ],
                                    }
                                }
                            },
                        },
                        "401": error_response,
                        "404": error_response,
                    },
                }
            },
            "/v1/environments/{name}/migrations": {
                "get": {
                    "summary": "List source migrations with target promotion status",
                    "security": bearer_auth,
                    "parameters": [
                        {"$ref": "#/components/parameters/EnvironmentName"},
                    ],
                    "responses": {
                        "200": {
                            "description": "Migrations",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "environment": {"type": "string"},
                                            "source_environment": {"type": "string"},
                                            "target_environment": {"type": "string"},
                                            "generated_at_ms": {"type": "integer"},
                                            "migrations": {
                                                "type": "array",
                                                "items": {
                                                    "$ref": "#/components/schemas/MigrationSummary"
                                                },
                                            },
                                        },
                                        "required": [
                                            "environment",
                                            "source_environment",
                                            "target_environment",
                                            "generated_at_ms",
                                            "migrations",
                                        ],
                                    }
                                }
                            },
                        },
                        "401": error_response,
                        "404": error_response,
                    },
                }
            },
            "/v1/environments/{name}/migrations/{version}": {
                "get": {
                    "summary": "Get one source migration with SQL statements",
                    "security": bearer_auth,
                    "parameters": [
                        {"$ref": "#/components/parameters/EnvironmentName"},
                        {
                            "name": "version",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Migration detail",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "environment": {"type": "string"},
                                            "migration": {
                                                "$ref": "#/components/schemas/MigrationDetail"
                                            },
                                        },
                                        "required": ["environment", "migration"],
                                    }
                                }
                            },
                        },
                        "401": error_response,
                        "404": error_response,
                    },
                }
            },
            "/v1/branches/active": {
                "get": {
                    "summary": "Get the active local branch",
                    "security": bearer_auth,
                    "responses": {"200": {"description": "Active branch", "content": json_content}},
                }
            },
            "/v1/branches/save": {
                "post": {
                    "summary": "Save the active local branch snapshot",
                    "security": bearer_auth,
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BranchSaveRequest"}
                            }
                        },
                    },
                    "responses": {
                        "202": {"$ref": "#/components/responses/JobAccepted"},
                        "400": error_response,
                        "401": error_response,
                    },
                }
            },
            "/v1/branches/{name}": {
                "parameters": [{"$ref": "#/components/parameters/BranchName"}],
                "get": {
                    "summary": "Get one local Supabase branch",
                    "security": bearer_auth,
                    "responses": {
                        "200": {"description": "Branch", "content": json_content},
                        "404": error_response,
                    },
                },
                "delete": {
                    "summary": "Delete one local Supabase branch snapshot",
                    "security": bearer_auth,
                    "responses": {
                        "200": {"description": "Deleted branch", "content": json_content},
                        "400": error_response,
                        "404": error_response,
                    },
                },
            },
            "/v1/branches/{name}/save": {
                "parameters": [{"$ref": "#/components/parameters/BranchName"}],
                "post": {
                    "summary": "Save one local Supabase branch snapshot",
                    "security": bearer_auth,
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BranchSaveRequest"}
                            }
                        },
                    },
                    "responses": {"202": {"$ref": "#/components/responses/JobAccepted"}},
                },
            },
            "/v1/branches/{name}/switch": {
                "parameters": [{"$ref": "#/components/parameters/BranchName"}],
                "post": {
                    "summary": "Restore a branch snapshot into the active local Supabase runtime",
                    "security": bearer_auth,
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BranchSwitchRequest"}
                            }
                        },
                    },
                    "responses": {"202": {"$ref": "#/components/responses/JobAccepted"}},
                },
            },
            "/v1/branches/{name}/merge": {
                "parameters": [{"$ref": "#/components/parameters/BranchName"}],
                "post": {
                    "summary": "Merge a feature branch snapshot into main",
                    "description": (
                        "Copies the source branch snapshot and migration ledger into "
                        "main, records merge metadata, and by default switches the "
                        "local runtime to main. Protected production targets can only "
                        "run migrations/up from main."
                    ),
                    "security": bearer_auth,
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BranchMergeRequest"}
                            }
                        },
                    },
                    "responses": {"202": {"$ref": "#/components/responses/JobAccepted"}},
                },
            },
            "/v1/branches/{name}/reset": {
                "parameters": [{"$ref": "#/components/parameters/BranchName"}],
                "post": {
                    "summary": "Replace a branch snapshot from another branch snapshot",
                    "security": bearer_auth,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BranchResetRequest"}
                            }
                        },
                    },
                    "responses": {"202": {"$ref": "#/components/responses/JobAccepted"}},
                },
            },
        }
    )
    return definition


def read_sync_api_definition():
    return add_sync_api_routes(json.loads(_base_sync_definition_file().read_text()))


def read_branching_doc():
    if BRANCHING_DOC_FILE.exists():
        return BRANCHING_DOC_FILE.read_text()
    local = Path(__file__).resolve().parents[1] / "branching.md"
    return local.read_text()


def read_import_doc():
    if IMPORT_DOC_FILE.exists():
        return IMPORT_DOC_FILE.read_text()
    local = Path(__file__).resolve().parents[1] / "imports.md"
    return local.read_text()
