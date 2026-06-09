from .settings import *
from .audit import now_ms
from .db import database_endpoint_from_config, psql_json, source_migrations_sql, target_promotion_records_sql
from .migration_display import *

def migration_summary(migration, target_record, source_environment, target_environment):
    target_record = target_record or {}
    return {
        "version": migration.get("version"),
        "name": migration.get("name"),
        "statement_count": migration_statement_count(migration),
        "status": target_record.get("status") or "pending",
        "source_environment": source_environment,
        "target_environment": target_record.get("target_environment") or target_environment,
        "batch_id": target_record.get("batch_id"),
        "batch_label": target_record.get("batch_label"),
        "started_at": target_record.get("started_at"),
        "promoted_at": target_record.get("promoted_at"),
        "duration_ms": target_record.get("duration_ms"),
        "error_message": target_record.get("error_message"),
    }


def collect_target_promotion_records(endpoint, source_environment):
    try:
        return psql_json(endpoint, target_promotion_records_sql(source_environment))
    except RuntimeError as exc:
        if "does not exist" in str(exc):
            return []
        raise


def environment_migrations(env_name, config):
    source_environment = config.get("source_env", "dev")
    target_environment = config.get("target_env", env_name)
    source_endpoint = database_endpoint_from_config(config, "source")
    target_endpoint = database_endpoint_from_config(config, "target")
    source_migrations = psql_json(source_endpoint, source_migrations_sql())
    target_records = collect_target_promotion_records(target_endpoint, source_environment)
    records_by_version = {
        item.get("source_version"): item for item in target_records if isinstance(item, dict)
    }
    migrations = [
        migration_summary(
            migration,
            records_by_version.get(migration.get("version")),
            source_environment,
            target_environment,
        )
        for migration in source_migrations
    ]
    return {
        "environment": env_name,
        "source_environment": source_environment,
        "target_environment": target_environment,
        "generated_at_ms": now_ms(),
        "migrations": migrations,
    }


def environment_migration_detail(env_name, config, version):
    source_environment = config.get("source_env", "dev")
    target_environment = config.get("target_env", env_name)
    source_endpoint = database_endpoint_from_config(config, "source")
    target_endpoint = database_endpoint_from_config(config, "target")
    source_migrations = psql_json(source_endpoint, source_migrations_sql())
    migration = next(
        (item for item in source_migrations if item.get("version") == version),
        None,
    )
    if migration is None:
        raise ValueError("Migration not found")
    target_records = collect_target_promotion_records(target_endpoint, source_environment)
    target_record = next(
        (item for item in target_records if item.get("source_version") == version),
        None,
    )
    detail = migration_summary(
        migration,
        target_record,
        source_environment,
        target_environment,
    )
    detail["statements"] = migration_display_statements(migration)
    detail["sql"] = migration_sql(migration)
    return {
        "environment": env_name,
        "migration": detail,
    }
