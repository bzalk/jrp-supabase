from .settings import *
from .audit import now_ms, mask_url
from .db import database_endpoint_from_config, endpoint_label, psql_json
from .database_sql import storage_bucket_stats_sql, table_stats_sql
from .env_config import (
    infer_project_ref_from_db_url,
    project_domain,
    project_metadata_from_config,
)
from .http_utils import parse_bool_body
from .management import management_api_json


IMPORT_DATABASE_MODES = {
    "schema-only",
    "schema-and-data",
}


def database_overview_sql():
    return f"""
set statement_timeout = {STATS_STATEMENT_TIMEOUT_MS};
with extension_items as (
  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'name', e.extname,
        'schema', n.nspname,
        'version', e.extversion
      )
      order by e.extname
    ),
    '[]'::jsonb
  ) as extensions
  from pg_extension e
  join pg_namespace n on n.oid = e.extnamespace
)
select jsonb_build_object(
  'database_name', current_database(),
  'current_user', current_user,
  'server_version', current_setting('server_version'),
  'server_version_num', current_setting('server_version_num')::integer,
  'database_size_bytes', pg_database_size(current_database())::bigint,
  'extensions', (select extensions from extension_items),
  'has_storage_buckets', to_regclass('storage.buckets') is not null,
  'has_supabase_migrations', to_regclass('supabase_migrations.schema_migrations') is not null
)::text;
"""


def import_tool_state_summary():
    return {
        "state_store": "sync-api-data",
        "state_store_kind": "filesystem",
        "data_dir": str(DATA_DIR),
        "environment_registry": str(ENVIRONMENTS_FILE),
        "branch_snapshot_dir": str(BRANCHES_DIR),
        "uses_source_database_for_tool_state": False,
        "uses_target_database_for_tool_state": False,
        "notes": (
            "sync-api control-plane state is stored in its own /data volume. "
            "Source and target Supabase databases are inspected or restored as "
            "external project databases, not used as the tool metadata store."
        ),
    }


def import_default_options(body):
    database_mode = body.get("database_mode", "schema-only")
    if database_mode not in IMPORT_DATABASE_MODES:
        raise ValueError(
            "database_mode must be one of: " + ", ".join(sorted(IMPORT_DATABASE_MODES))
        )

    return {
        "database_mode": database_mode,
        "include_storage_bucket_metadata": parse_bool_body(
            body, "include_storage_bucket_metadata", True
        ),
        "include_storage_objects": parse_bool_body(body, "include_storage_objects", False),
        "include_edge_functions": parse_bool_body(body, "include_edge_functions", True),
        "include_auth_data": parse_bool_body(
            body,
            "include_auth_data",
            database_mode == "schema-and-data",
        ),
        "exact_rows": parse_bool_body(body, "exact_rows", False),
        "include_columns": parse_bool_body(body, "include_columns", False),
        "largest_table_limit": parse_positive_int(
            body.get("largest_table_limit", 20),
            "largest_table_limit",
        ),
    }


def parse_positive_int(value, key):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def side_body(body, role):
    value = body.get(role)
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{role} must be an object")
    return value


def first_present(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def import_side_config(body, role):
    side = side_body(body, role)
    prefix = f"{role}_"
    project_ref = first_present(
        side.get("project_ref"),
        side.get("project_id"),
        body.get(prefix + "project_ref"),
        body.get(prefix + "project_id"),
    )
    db_url = first_present(side.get("db_url"), body.get(prefix + "db_url"))
    if not project_ref:
        project_ref = infer_project_ref_from_db_url(db_url)

    config = {
        "role": role,
        "label": first_present(side.get("env"), side.get("label"), role),
        "type": first_present(side.get("type"), body.get(prefix + "type")),
        "project_ref": project_ref,
        "db_url": db_url,
        "container": first_present(side.get("container"), body.get(prefix + "container")),
        "user": first_present(side.get("user"), body.get(prefix + "user")),
        "reset_user": first_present(side.get("reset_user"), body.get(prefix + "reset_user")),
        "db_name": first_present(side.get("db_name"), body.get(prefix + "db_name")),
        "access_token": first_present(
            side.get("access_token"),
            body.get(prefix + "access_token"),
            body.get("access_token"),
            os.environ.get("SUPABASE_ACCESS_TOKEN"),
        ),
        "api_base": first_present(
            side.get("api_base"),
            side.get("functions_api_base"),
            body.get(prefix + "api_base"),
            body.get(prefix + "functions_api_base"),
            FUNCTIONS_API_BASE,
        ).rstrip("/"),
    }

    if role == "target" and config["type"] == "local" and not config["db_url"] and not config["container"]:
        config["container"] = "supabase-db"

    return config


def side_environment_config(side):
    role = side["role"]
    return {
        f"{role}_db_url": side.get("db_url"),
        f"{role}_container": side.get("container"),
        f"{role}_user": side.get("user"),
        f"{role}_reset_user": side.get("reset_user"),
        f"{role}_db_name": side.get("db_name"),
        f"{role}_project_ref": side.get("project_ref"),
        f"{role}_supabase_access_token": side.get("access_token"),
        f"{role}_functions_api_base": side.get("api_base"),
    }


def import_side_has_connection_or_project(side):
    return bool(side.get("project_ref") or side.get("db_url") or side.get("container"))


def public_import_side(side):
    return {
        "role": side["role"],
        "label": side["label"],
        "type": side.get("type"),
        "project_ref": side.get("project_ref"),
        "domain": project_domain(side.get("project_ref")),
        "db_url": mask_url(side.get("db_url")),
        "container": side.get("container"),
        "db_name": side.get("db_name"),
        "api_base": side.get("api_base"),
        "has_access_token": bool(side.get("access_token")),
    }


def project_metadata_for_import(side):
    project_ref = side.get("project_ref")
    if not project_ref:
        return {
            "available": False,
            "error": "Project ref is not configured or inferable",
        }
    if not side.get("access_token"):
        return {
            "available": False,
            "project_ref": project_ref,
            "error": "Supabase access token is not configured",
        }

    config = side_environment_config(side)
    metadata, error = project_metadata_from_config(config, side["role"], project_ref)
    return {
        "available": metadata is not None,
        "project_ref": project_ref,
        "metadata": metadata,
        "error": error,
    }


def database_endpoint_for_import_side(side):
    role = side["role"]
    config = side_environment_config(side)
    if not side.get("db_url") and not side.get("container"):
        return None
    return database_endpoint_from_config(config, role)


def safe_database_plan(side, options):
    endpoint = database_endpoint_for_import_side(side)
    if endpoint is None:
        return {
            "available": False,
            "error": "Database connection is not configured",
        }

    try:
        overview = psql_json(endpoint, database_overview_sql())
        table_payload = psql_json(
            endpoint,
            table_stats_sql(options["exact_rows"], options["include_columns"]),
        )
        has_storage_buckets = table_payload.pop("has_storage_buckets", False)
        storage_buckets = []
        if has_storage_buckets:
            storage_buckets = psql_json(endpoint, storage_bucket_stats_sql())
        tables = table_payload.get("tables") or []
        largest_tables = sorted(
            tables,
            key=lambda item: item.get("total_bytes") or 0,
            reverse=True,
        )[: options["largest_table_limit"]]

        return {
            "available": True,
            "endpoint": endpoint_label(endpoint),
            "overview": overview,
            "table_count": table_payload.get("table_count", 0),
            "estimated_total_table_bytes": sum(
                item.get("total_bytes") or 0 for item in tables
            ),
            "largest_tables": largest_tables,
            "storage_bucket_count": len(storage_buckets),
            "storage_buckets": storage_buckets,
        }
    except Exception as exc:
        return {
            "available": False,
            "endpoint": endpoint_label(endpoint),
            "error": str(exc),
        }


def major_version(server_version_num):
    if not isinstance(server_version_num, int):
        return None
    return server_version_num // 10000


def extension_names(database_plan):
    extensions = (
        ((database_plan or {}).get("overview") or {}).get("extensions")
        if database_plan
        else []
    )
    return {
        item.get("name")
        for item in extensions or []
        if isinstance(item, dict) and item.get("name")
    }


def build_import_warnings(source_db, target_db, options):
    warnings = []
    if options["include_storage_objects"]:
        warnings.append(
            {
                "code": "storage_objects_separate",
                "message": (
                    "Storage object bytes are not part of database backup/restore. "
                    "They must be copied separately through the Storage/S3 API."
                ),
                "severity": "warning",
            }
        )
    else:
        warnings.append(
            {
                "code": "storage_objects_not_included",
                "message": (
                    "Storage bucket metadata can be planned from the database, but "
                    "storage object bytes are excluded unless include_storage_objects is true."
                ),
                "severity": "info",
            }
        )

    if options["database_mode"] == "schema-only" and options["include_auth_data"]:
        warnings.append(
            {
                "code": "auth_data_requires_table_data",
                "message": "Auth user data is table data and is not imported in schema-only mode.",
                "severity": "warning",
            }
        )

    if source_db.get("available") and target_db.get("available"):
        source_version = major_version(
            (source_db.get("overview") or {}).get("server_version_num")
        )
        target_version = major_version(
            (target_db.get("overview") or {}).get("server_version_num")
        )
        if source_version and target_version and source_version > target_version:
            warnings.append(
                {
                    "code": "postgres_version_downgrade",
                    "message": (
                        f"Source Postgres major version {source_version} is newer "
                        f"than target {target_version}; restore may need filtering."
                    ),
                    "severity": "warning",
                }
            )

        missing_extensions = sorted(extension_names(source_db) - extension_names(target_db))
        if missing_extensions:
            warnings.append(
                {
                    "code": "missing_target_extensions",
                    "message": "Target is missing installed source extensions.",
                    "severity": "warning",
                    "extensions": missing_extensions,
                }
            )

    return warnings


def feasibility_from_plan(source_db, target_db, options):
    can_plan = source_db.get("available") is True
    target_ready = target_db.get("available") is True
    return {
        "can_plan_import": can_plan,
        "can_run_platform_to_local_now": can_plan and target_ready,
        "schema_only_supported": can_plan,
        "schema_and_data_supported": can_plan and options["database_mode"] == "schema-and-data",
        "requires_storage_object_copy": bool(options["include_storage_objects"]),
        "requires_supabase_cli_dump": True,
        "requires_tool_database": False,
    }


def build_import_plan(body):
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")

    options = import_default_options(body)
    source = import_side_config(body, "source")
    target = import_side_config(body, "target")
    if not import_side_has_connection_or_project(source):
        raise ValueError("source must include project_ref, db_url, or container")

    source_db = safe_database_plan(source, options)
    target_db = safe_database_plan(target, options)
    warnings = build_import_warnings(source_db, target_db, options)

    return {
        "generated_at_ms": now_ms(),
        "kind": "import_plan",
        "control_plane": import_tool_state_summary(),
        "options": options,
        "source": {
            "side": public_import_side(source),
            "project": project_metadata_for_import(source),
            "database": source_db,
        },
        "target": {
            "side": public_import_side(target),
            "project": project_metadata_for_import(target),
            "database": target_db,
        },
        "warnings": warnings,
        "feasibility": feasibility_from_plan(source_db, target_db, options),
        "next_recommended_endpoint": "POST /v1/imports/platform-to-local",
    }


def supabase_access_token_from_handler(handler):
    return (
        handler.headers.get("X-Supabase-Access-Token")
        or handler.headers.get("X-SUPABASE-ACCESS-TOKEN")
        or os.environ.get("SUPABASE_ACCESS_TOKEN")
    )


def supabase_management_endpoint(access_token):
    return {
        "api_base": FUNCTIONS_API_BASE,
        "project_ref": None,
        "access_token": access_token,
    }


def require_supabase_access_token(handler):
    token = supabase_access_token_from_handler(handler)
    if not token:
        raise ValueError(
            "Supabase access token is required. Send X-Supabase-Access-Token or configure SUPABASE_ACCESS_TOKEN."
        )
    return token


def list_supabase_organizations(handler):
    token = require_supabase_access_token(handler)
    payload = management_api_json(supabase_management_endpoint(token), "/v1/organizations")
    return {
        "generated_at_ms": now_ms(),
        "organizations": payload.get("organizations", payload)
        if isinstance(payload, dict)
        else payload,
    }


def list_supabase_projects(handler):
    token = require_supabase_access_token(handler)
    query = parse_qs(urlparse(handler.path).query)
    organization_id = (query.get("organization_id") or query.get("organization_slug") or [None])[-1]
    path = "/v1/projects"
    if organization_id:
        path += "?organization_id=" + quote(organization_id, safe="")
    payload = management_api_json(supabase_management_endpoint(token), path)
    return {
        "generated_at_ms": now_ms(),
        "projects": payload.get("projects", payload) if isinstance(payload, dict) else payload,
    }


def get_supabase_project(handler, ref):
    token = require_supabase_access_token(handler)
    payload = management_api_json(
        supabase_management_endpoint(token),
        f"/v1/projects/{quote(ref, safe='')}",
    )
    return {
        "generated_at_ms": now_ms(),
        "project": payload,
    }


def list_supabase_project_backups(handler, ref):
    token = require_supabase_access_token(handler)
    payload = management_api_json(
        supabase_management_endpoint(token),
        f"/v1/projects/{quote(ref, safe='')}/database/backups",
    )
    return {
        "generated_at_ms": now_ms(),
        "project_ref": ref,
        "backups": payload.get("backups", payload) if isinstance(payload, dict) else payload,
    }
