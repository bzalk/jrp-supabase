from .settings import *


def _base_openapi_file():
    if OPENAPI_FILE.exists():
        return OPENAPI_FILE
    local = Path(__file__).resolve().parents[1] / "openapi.json"
    if local.exists():
        return local
    raise FileNotFoundError(f"OpenAPI file not found: {OPENAPI_FILE}")

def add_branch_openapi(definition):
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
        }
    )

    paths.update(
        {
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


def read_openapi_definition():
    return add_branch_openapi(json.loads(_base_openapi_file().read_text()))


def read_branching_doc():
    if BRANCHING_DOC_FILE.exists():
        return BRANCHING_DOC_FILE.read_text()
    local = Path(__file__).resolve().parents[1] / "branching.md"
    return local.read_text()
