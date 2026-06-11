from .settings import *
from .audit import *
from .db import *
from .database import *
from .edge_functions import *
from .env_config import *
from .http_utils import parse_bool_body

def command_for_environment(config, action, body):
    command = [PROMOTE_SCRIPT]

    source_db_url = config.get("source_db_url")
    source_container = config.get("source_container", "supabase-db")
    if source_db_url:
        command += ["--source-db-url", source_db_url]
    else:
        command += ["--source-container", source_container]
        if config.get("source_user"):
            command += ["--source-user", config["source_user"]]
        if config.get("source_db_name"):
            command += ["--source-db-name", config["source_db_name"]]

    target_db_url = config.get("target_db_url")
    target_container = config.get("target_container")
    if target_db_url:
        command += ["--target-db-url", target_db_url]
    elif target_container:
        command += ["--target-container", target_container]
        if config.get("target_user"):
            command += ["--target-user", config["target_user"]]
        if config.get("target_db_name"):
            command += ["--target-db-name", config["target_db_name"]]
    else:
        raise ValueError("Environment must define target_db_url or target_container")

    command += ["--source-env", config.get("source_env", "dev")]
    command += ["--target-env", config.get("target_env", config.get("name", "target"))]

    batch_label = body.get("batch_label") or config.get("batch_label")
    if batch_label:
        command += ["--batch-label", str(batch_label)]

    limit = body.get("limit")
    if limit is not None:
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        command += ["--limit", str(limit)]

    sync_buckets = body.get(
        "sync_storage_buckets", config.get("sync_storage_buckets", True)
    )
    if sync_buckets is False:
        command += ["--no-sync-storage-buckets"]

    if action in ("plan", "validate"):
        command += ["--dry-run"]

    return command

def environment_stats(env_name, config, exact_rows, include_columns):
    source = collect_database_stats(
        "source",
        config.get("source_env", "dev"),
        database_endpoint_from_config(config, "source"),
        exact_rows,
        include_columns,
    )
    target = collect_database_stats(
        "target",
        config.get("target_env", env_name),
        database_endpoint_from_config(config, "target"),
        exact_rows,
        include_columns,
    )
    return {
        "environment": env_name,
        "generated_at_ms": now_ms(),
        "exact_rows": exact_rows,
        "databases": [source, target],
        "comparison": compare_database_stats(source, target),
    }


def environment_table_stats(env_name, config, schema_name, table_name, exact_rows):
    source = collect_table_stats(
        "source",
        config.get("source_env", "dev"),
        database_endpoint_from_config(config, "source"),
        schema_name,
        table_name,
        exact_rows,
    )
    target = collect_table_stats(
        "target",
        config.get("target_env", env_name),
        database_endpoint_from_config(config, "target"),
        schema_name,
        table_name,
        exact_rows,
    )
    return {
        "environment": env_name,
        "generated_at_ms": now_ms(),
        "schema": schema_name,
        "table": table_name,
        "exact_rows": exact_rows,
        "tables": [source, target],
    }


def environment_database_functions(env_name, config):
    source = collect_database_functions(
        "source",
        config.get("source_env", "dev"),
        database_endpoint_from_config(config, "source"),
    )
    target = collect_database_functions(
        "target",
        config.get("target_env", env_name),
        database_endpoint_from_config(config, "target"),
    )
    return {
        "environment": env_name,
        "generated_at_ms": now_ms(),
        "databases": [source, target],
        "comparison": compare_database_functions(source, target),
    }


def environment_database_triggers(env_name, config, include_internal):
    source = collect_database_triggers(
        "source",
        config.get("source_env", "dev"),
        database_endpoint_from_config(config, "source"),
        include_internal,
    )
    target = collect_database_triggers(
        "target",
        config.get("target_env", env_name),
        database_endpoint_from_config(config, "target"),
        include_internal,
    )
    return {
        "environment": env_name,
        "generated_at_ms": now_ms(),
        "include_internal": include_internal,
        "databases": [source, target],
        "comparison": compare_database_triggers(source, target),
    }


def environment_edge_functions(env_name, config):
    source = collect_edge_functions_side(
        "source",
        config.get("source_env", "dev"),
        config,
    )
    target = collect_edge_functions_side(
        "target",
        config.get("target_env", env_name),
        config,
    )
    return {
        "environment": env_name,
        "generated_at_ms": now_ms(),
        "edge_functions": [source, target],
        "comparison": compare_edge_functions(source, target),
    }


def environment_edge_function(env_name, config, identifier, include_source):
    source = get_edge_function_side(
        "source",
        config.get("source_env", "dev"),
        config,
        identifier,
        include_source,
    )
    target = get_edge_function_side(
        "target",
        config.get("target_env", env_name),
        config,
        identifier,
        include_source,
    )
    response = {
        "environment": env_name,
        "generated_at_ms": now_ms(),
        "edge_function": identifier,
        "include_source": include_source,
        "sources": [source, target],
    }

    source_function = source.get("function") or {}
    target_function = target.get("function") or {}
    source_hash = edge_function_digest(source_function)
    target_hash = edge_function_digest(target_function)
    if source_hash and target_hash:
        response["comparison"] = {
            "source_sha256": source_hash,
            "target_sha256": target_hash,
            "source_matches_target": source_hash == target_hash,
        }
    return response


def environment_metadata(env_name, config, include_internal_triggers):
    return {
        "environment": env_name,
        "generated_at_ms": now_ms(),
        "database_functions": environment_database_functions(env_name, config),
        "database_triggers": environment_database_triggers(
            env_name,
            config,
            include_internal_triggers,
        ),
        "edge_functions": environment_edge_functions(env_name, config),
    }


def reset_confirmation_for_role(target_role):
    if target_role == "source":
        return "RESET SOURCE"
    if target_role == "target":
        return "RESET DESTINATION"
    raise ValueError("reset side must be source or destination")


def parse_reset_options(body, target_role):
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")

    expected_confirmation = reset_confirmation_for_role(target_role)
    if body.get("confirm") != expected_confirmation:
        raise ValueError(f'confirm must be exactly "{expected_confirmation}"')

    reset_database = parse_bool_body(body, "reset_database", True)
    reset_edge_functions = parse_bool_body(body, "reset_edge_functions", True)
    if not reset_database and not reset_edge_functions:
        raise ValueError("At least one of reset_database or reset_edge_functions must be true")

    return {
        "dry_run": parse_bool_body(body, "dry_run", False),
        "reset_database": reset_database,
        "reset_edge_functions": reset_edge_functions,
        "prune_edge_functions": parse_bool_body(body, "prune_edge_functions", True),
        "drop_target_schemas": parse_bool_body(body, "drop_target_schemas", True),
        "no_owner": parse_bool_body(body, "no_owner", True),
        "no_privileges": parse_bool_body(body, "no_privileges", True),
    }


def reset_source_role(target_role):
    return "target" if target_role == "source" else "source"


def reset_role_label(config, env_name, role):
    return config.get(f"{role}_env", "dev" if role == "source" else env_name)


def reset_command_summary(config, env_name, target_role, options):
    source_role = reset_source_role(target_role)
    command = [
        "sync-api-reset",
        "--from",
        reset_role_label(config, env_name, source_role),
        "--to",
        reset_role_label(config, env_name, target_role),
    ]
    if options["dry_run"]:
        command.append("--dry-run")
    if options["reset_database"]:
        command.append("--database")
    if options["reset_edge_functions"]:
        command.append("--edge-functions")
    if options["prune_edge_functions"]:
        command.append("--prune-edge-functions")
    return command


def should_restore_managed_table_data(schema_name, table_name, config, options):
    if schema_name == "storage":
        return (
            table_name == "buckets"
            and config.get("sync_storage_buckets", True) is not False
            and options.get("include_storage_bucket_metadata", True) is not False
        )
    if schema_name in {"realtime", "_realtime"}:
        return False
    return True


def copy_migration_ledger(job_id, source_endpoint, target_endpoint):
    try:
        migrations = psql_json(source_endpoint, source_migrations_sql())
    except RuntimeError as exc:
        if "does not exist" in str(exc):
            append_job_output(
                job_id,
                "Source migration ledger was not found; no migrations copied\n",
            )
            return
        raise

    append_job_output(
        job_id,
        f"Copying migration ledger with {len(migrations)} migration(s)\n",
    )
    run_logged_sql(job_id, target_endpoint, migration_ledger_restore_sql(migrations))


def reset_database_copy(job_id, config, env_name, source_role, target_role, options):
    source_endpoint = reset_database_endpoint_from_config(config, source_role)
    target_endpoint = reset_database_endpoint_from_config(config, target_role)
    append_job_output(
        job_id,
        (
            "Database reset: "
            f"{reset_role_label(config, env_name, source_role)} "
            f"({endpoint_label(source_endpoint)}) -> "
            f"{reset_role_label(config, env_name, target_role)} "
            f"({endpoint_label(target_endpoint)})\n"
        ),
    )

    RESET_TMP_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = None
    list_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f"reset-{job_id}-",
            suffix=".dump",
            dir=RESET_TMP_DIR,
            delete=False,
        ) as archive_file:
            archive_path = archive_file.name

        run_logged_command_to_file(
            job_id,
            pg_dump_command(source_endpoint, options),
            archive_path,
        )

        schema_ownership = psql_json(target_endpoint, reset_schema_ownership_sql())
        managed_schemas = [
            item["schema"]
            for item in schema_ownership
            if not item.get("can_drop")
        ]
        use_managed_schema_restore = bool(managed_schemas)

        if options.get("drop_target_schemas", True):
            append_job_output(
                job_id,
                "Dropping target-owned non-system, non-extension schemas before restore\n",
            )
            run_logged_sql(
                job_id,
                target_endpoint,
                reset_preclean_sql(drop_only_owned=use_managed_schema_restore),
            )

        if use_managed_schema_restore:
            append_job_output(
                job_id,
                (
                    "Preserving platform-owned schemas that this connection cannot "
                    f"drop: {', '.join(managed_schemas)}\n"
                ),
            )
            append_job_output(
                job_id,
                "Truncating preserved schemas before restoring their table data\n",
            )
            managed_table_privileges = psql_json(
                target_endpoint,
                managed_table_privileges_sql(managed_schemas),
            )
            resettable_managed_tables = {
                (item["schema"], item["table"])
                for item in managed_table_privileges
                if item.get("can_insert") and item.get("can_truncate")
            }
            restore_managed_table_data = {
                table_key
                for table_key in resettable_managed_tables
                if should_restore_managed_table_data(
                    table_key[0], table_key[1], config, options
                )
            }
            skipped_managed_tables = [
                f"{item['schema']}.{item['table']}"
                for item in managed_table_privileges
                if not (item.get("can_insert") and item.get("can_truncate"))
            ]
            if skipped_managed_tables:
                append_job_output(
                    job_id,
                    (
                        "Skipping protected managed tables that this connection "
                        "cannot reset: "
                        + ", ".join(skipped_managed_tables)
                        + "\n"
                    ),
                )
            skipped_managed_table_data = sorted(
                resettable_managed_tables - restore_managed_table_data
            )
            if skipped_managed_table_data:
                append_job_output(
                    job_id,
                    (
                        "Skipping managed table data that is not safe to restore "
                        "for this import mode: "
                        + ", ".join(
                            f"{schema_name}.{table_name}"
                            for schema_name, table_name in skipped_managed_table_data
                        )
                        + "\n"
                    ),
                )
            managed_sequence_privileges = psql_json(
                target_endpoint,
                managed_sequence_privileges_sql(managed_schemas),
            )
            resettable_managed_sequences = {
                (item["schema"], item["sequence"])
                for item in managed_sequence_privileges
                if item.get("can_update")
            }
            skipped_managed_sequences = [
                f"{item['schema']}.{item['sequence']}"
                for item in managed_sequence_privileges
                if not item.get("can_update")
            ]
            if skipped_managed_sequences:
                append_job_output(
                    job_id,
                    (
                        "Skipping protected managed sequences that this connection "
                        "cannot reset: "
                        + ", ".join(skipped_managed_sequences)
                        + "\n"
                    ),
                )
            run_logged_sql(
                job_id,
                target_endpoint,
                truncate_tables_sql(resettable_managed_tables, restart_identity=False),
            )
            append_job_output(
                job_id,
                "Ensuring local platform compatibility schemas exist before restore\n",
            )
            run_logged_sql(
                job_id,
                target_endpoint,
                reset_ensure_import_platform_schemas_sql(),
            )
            append_job_output(
                job_id,
                "Dropping resettable app-owned objects in preserved app schemas before restore\n",
            )
            run_logged_sql(
                job_id,
                target_endpoint,
                reset_app_schema_objects_sql(["public"]),
            )
            preserved_schemas = psql_json(target_endpoint, reset_existing_schema_names_sql())
            existing_extensions = psql_json(
                target_endpoint,
                reset_existing_extension_names_sql(),
            )
            existing_event_triggers = psql_json(
                target_endpoint,
                reset_existing_event_trigger_names_sql(),
            )
            existing_publications = psql_json(
                target_endpoint,
                reset_existing_publication_names_sql(),
            )

            with tempfile.NamedTemporaryFile(
                prefix=f"reset-{job_id}-",
                suffix=".list",
                dir=RESET_TMP_DIR,
                delete=False,
            ) as list_file:
                list_path = list_file.name
            removed_count = write_filtered_restore_list(
                archive_path,
                list_path,
                managed_schemas,
                preserved_schemas,
                restore_managed_table_data,
                resettable_managed_sequences,
                existing_extensions,
                existing_event_triggers,
                existing_publications,
                SKIP_SOURCE_PLATFORM_SCHEMAS_FOR_HOSTED_RESTORE,
                SKIP_SOURCE_PLATFORM_EXTENSIONS_FOR_RESTORE,
            )
            append_job_output(
                job_id,
                (
                    "Filtered restore list for platform-owned schemas; "
                    f"skipped {removed_count} protected/schema-definition items "
                    f"and kept {len(restore_managed_table_data)} managed table-data items "
                    f"and {len(resettable_managed_sequences)} resettable managed sequences. "
                    f"Preserved existing target schemas: {', '.join(preserved_schemas)}. "
                    "Skipped hosted platform schemas from source dump: "
                    f"{', '.join(sorted(SKIP_SOURCE_PLATFORM_SCHEMAS_FOR_HOSTED_RESTORE))}. "
                    "Skipped hosted platform extensions from source dump: "
                    f"{', '.join(sorted(SKIP_SOURCE_PLATFORM_EXTENSIONS_FOR_RESTORE))}.\n"
                ),
            )

        run_logged_command(
            job_id,
            pg_restore_command(
                target_endpoint,
                options,
                clean=not use_managed_schema_restore,
                use_list=list_path,
                disable_trigger_checks=(
                    use_managed_schema_restore
                    and target_endpoint["kind"] == "container"
                    and options.get("include_table_data", True)
                ),
                filter_unsupported_settings=True,
            ),
            input_path=archive_path,
        )
        if options.get("copy_migration_ledger"):
            copy_migration_ledger(job_id, source_endpoint, target_endpoint)
    finally:
        if archive_path:
            Path(archive_path).unlink(missing_ok=True)
        if list_path:
            Path(list_path).unlink(missing_ok=True)


def list_edge_functions_for_reset(endpoint):
    if endpoint["kind"] == "local":
        return list_local_edge_functions(endpoint["root"])
    return list_remote_edge_functions(endpoint)


def get_edge_function_source_for_reset(endpoint, slug):
    if endpoint["kind"] == "local":
        return local_edge_function_payload(
            endpoint["root"],
            slug,
            include_files=True,
            include_content=True,
            max_file_bytes=None,
        )
    return get_remote_edge_function_source(endpoint, slug, max_file_bytes=None)


def put_edge_function_for_reset(endpoint, function_item):
    if endpoint["kind"] == "local":
        write_local_edge_function(endpoint["root"], function_item)
    else:
        deploy_remote_edge_function(endpoint, function_item)


def delete_edge_function_for_reset(endpoint, slug):
    if endpoint["kind"] == "local":
        delete_local_edge_function(endpoint["root"], slug)
    else:
        delete_remote_edge_function(endpoint, slug)


def reset_edge_functions(job_id, config, env_name, source_role, target_role, options):
    source_endpoint = edge_functions_endpoint_from_config(config, source_role)
    target_endpoint = edge_functions_endpoint_from_config(config, target_role)
    if not source_endpoint:
        raise RuntimeError(f"{source_role} edge function metadata is not configured")
    if not target_endpoint:
        raise RuntimeError(f"{target_role} edge function metadata is not configured")

    append_job_output(
        job_id,
        (
            "Edge Functions reset: "
            f"{reset_role_label(config, env_name, source_role)} -> "
            f"{reset_role_label(config, env_name, target_role)}\n"
        ),
    )
    source_functions = list_edge_functions_for_reset(source_endpoint)
    target_functions = list_edge_functions_for_reset(target_endpoint)
    source_slugs = {
        edge_function_slug(function_item)
        for function_item in source_functions
        if edge_function_slug(function_item)
    }
    target_slugs = {
        edge_function_slug(function_item)
        for function_item in target_functions
        if edge_function_slug(function_item)
    }

    for slug in sorted(source_slugs):
        append_job_output(job_id, f"Deploying edge function {slug}\n")
        function_item = get_edge_function_source_for_reset(source_endpoint, slug)
        put_edge_function_for_reset(target_endpoint, function_item)

    if options.get("prune_edge_functions", True):
        for slug in sorted(target_slugs - source_slugs):
            append_job_output(job_id, f"Deleting extra edge function {slug}\n")
            delete_edge_function_for_reset(target_endpoint, slug)


def run_reset_job(job_id, env_name, config, target_role, options):
    source_role = reset_source_role(target_role)
    append_job_output(
        job_id,
        (
            f"Reset job started for {env_name}: "
            f"{reset_role_label(config, env_name, target_role)} will match "
            f"{reset_role_label(config, env_name, source_role)}\n"
        ),
    )

    if options["dry_run"]:
        append_job_output(job_id, "Dry run only; no changes will be written.\n")
        if options["reset_database"]:
            source_endpoint = reset_database_endpoint_from_config(config, source_role)
            target_endpoint = reset_database_endpoint_from_config(config, target_role)
            append_job_output(
                job_id,
                (
                    "Would reset database: "
                    f"{endpoint_label(source_endpoint)} -> {endpoint_label(target_endpoint)}\n"
                ),
            )
        if options["reset_edge_functions"]:
            source_endpoint = edge_functions_endpoint_from_config(config, source_role)
            target_endpoint = edge_functions_endpoint_from_config(config, target_role)
            if not source_endpoint or not target_endpoint:
                raise RuntimeError("Both sides must have edge functions configured")
            source_functions = list_edge_functions_for_reset(source_endpoint)
            target_functions = list_edge_functions_for_reset(target_endpoint)
            source_slugs = sorted(
                slug
                for slug in (edge_function_slug(item) for item in source_functions)
                if slug
            )
            target_slugs = sorted(
                slug
                for slug in (edge_function_slug(item) for item in target_functions)
                if slug
            )
            append_job_output(
                job_id,
                (
                    f"Would deploy/update {len(source_slugs)} edge functions "
                    f"and prune {len(set(target_slugs) - set(source_slugs))} extras.\n"
                ),
            )
        return

    if options["reset_database"]:
        reset_database_copy(job_id, config, env_name, source_role, target_role, options)
    if options["reset_edge_functions"]:
        reset_edge_functions(job_id, config, env_name, source_role, target_role, options)

    append_job_output(job_id, "Reset job completed.\n")
