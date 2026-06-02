from .settings import *
from .audit import now_ms
from .db import database_endpoint_from_config, psql_json
from .branches import APP_ONLY_EXCLUDED_SCHEMAS, branch_database_endpoint
from .http_utils import parse_string_query, split_config_list
from .database_sql import *

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_text(value):
    return sha256_bytes(value.encode("utf-8"))


def add_definition_hashes(items):
    for item in items:
        definition = item.get("definition")
        if isinstance(definition, str):
            item["definition_sha256"] = sha256_text(definition)


def collect_database_functions(role, environment, endpoint):
    payload = psql_json(endpoint, database_functions_sql())
    add_definition_hashes(payload["functions"])
    return {
        "role": role,
        "environment": environment,
        **payload,
    }


def collect_database_triggers(role, environment, endpoint, include_internal):
    payload = psql_json(endpoint, database_triggers_sql(include_internal))
    add_definition_hashes(payload["table_triggers"])
    add_definition_hashes(payload["event_triggers"])
    return {
        "role": role,
        "environment": environment,
        **payload,
    }


def collect_database_stats(role, environment, endpoint, exact_rows, include_columns):
    table_payload = psql_json(endpoint, table_stats_sql(exact_rows, include_columns))
    buckets = []
    if table_payload.pop("has_storage_buckets", False):
        buckets = psql_json(endpoint, storage_bucket_stats_sql())

    return {
        "role": role,
        "environment": environment,
        "database_name": table_payload["database_name"],
        "table_count": table_payload["table_count"],
        "tables": table_payload["tables"],
        "storage_bucket_count": len(buckets),
        "storage_buckets": buckets,
    }


def collect_table_stats(role, environment, endpoint, schema_name, table_name, exact_rows):
    payload = psql_json(endpoint, table_metadata_sql(schema_name, table_name, exact_rows))
    payload["role"] = role
    payload["environment"] = environment
    return payload


def annotate_database_schemas(payload):
    default_app_schemas = split_config_list(BRANCH_DEFAULT_APP_SCHEMAS) or ["public"]
    schemas = payload.get("schemas") or []
    for item in schemas:
        excluded = item.get("name") in APP_ONLY_EXCLUDED_SCHEMAS
        item["excluded_from_app_only"] = excluded
        item["selectable_for_app_only"] = not excluded
    return {
        "database_name": payload.get("database_name"),
        "schemas": schemas,
        "default_app_schemas": default_app_schemas,
        "app_only_excluded_schemas": APP_ONLY_EXCLUDED_SCHEMAS,
    }


def collect_database_schemas(role, environment, endpoint):
    return {
        "role": role,
        "environment": environment,
        **annotate_database_schemas(psql_json(endpoint, database_schemas_sql())),
    }


def branch_database_schemas():
    return {
        "generated_at_ms": now_ms(),
        "database": collect_database_schemas(
            "local",
            "current",
            branch_database_endpoint(),
        ),
    }


def parse_schema_roles(query):
    role = parse_string_query(query, "role", "both").lower()
    if role == "both":
        return ["source", "target"]
    if role in ("source", "target"):
        return [role]
    raise ValueError("role must be source, target, or both")


def environment_schemas(env_name, config, roles):
    databases = [
        collect_database_schemas(
            role,
            config.get(f"{role}_env", "dev" if role == "source" else env_name),
            database_endpoint_from_config(config, role),
        )
        for role in roles
    ]
    return {
        "environment": env_name,
        "generated_at_ms": now_ms(),
        "databases": databases,
    }
