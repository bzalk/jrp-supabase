from .settings import *
from .audit import *
from .db import *
from .branch_core import *
from .branch_services import *

def branch_pg_options(
    mode,
    schemas=None,
    no_owner=True,
    no_privileges=False,
    include_table_data=True,
):
    options = {
        "no_owner": no_owner,
        "no_privileges": no_privileges,
        "include_table_data": include_table_data,
    }
    if mode == "app-only":
        options["schemas"] = schemas or validate_schema_names(
            BRANCH_DEFAULT_APP_SCHEMAS,
            default=["public"],
        )
    return options


def branch_snapshot_metadata(
    name,
    mode,
    include_storage_files,
    schemas,
    notes,
    existing=None,
    include_table_data=True,
):
    now = now_iso()
    metadata = dict(existing or {})
    data_mode = "schema-and-data" if include_table_data else "schema-only"
    metadata.update(
        {
            "name": name,
            "mode": mode,
            "updated_at": now,
            "includes_database": True,
            "includes_table_data": include_table_data,
            "data_mode": data_mode,
            "includes_storage_files": include_storage_files,
            "schemas": ["*"] if mode == "full" else schemas,
            "dump_file": branch_dump_filename(mode),
            "migration_ledger_file": "schema_migrations.json",
        }
    )
    metadata.setdefault("created_at", now)
    if mode == "app-only":
        metadata["excluded_schemas"] = APP_ONLY_EXCLUDED_SCHEMAS
    else:
        metadata.pop("excluded_schemas", None)
    if include_storage_files:
        metadata["storage_file"] = "storage.tar.gz"
    else:
        metadata.pop("storage_file", None)
    if notes is not None:
        metadata["notes"] = notes
    elif "notes" not in metadata:
        if mode == "full":
            metadata["notes"] = (
                "Full local Supabase state snapshot"
                if include_table_data
                else "Full local Supabase schema-only branch"
            )
        else:
            metadata["notes"] = (
                "Application-only schema/data branch"
                if include_table_data
                else "Application-only schema-only branch"
            )
    return metadata


def snapshot_branch_state(
    job_id,
    name,
    mode,
    include_storage_files,
    schemas,
    notes=None,
    existing_metadata=None,
    overwrite=False,
    manage_services=True,
    no_owner=True,
    no_privileges=False,
    source_branch=None,
    include_table_data=True,
):
    target_dir = branch_dir(name)
    if target_dir.exists() and not overwrite:
        raise ValueError("Branch already exists")
    target_dir.mkdir(parents=True, exist_ok=True)

    metadata = branch_snapshot_metadata(
        name,
        mode,
        include_storage_files,
        schemas,
        notes,
        existing=existing_metadata,
        include_table_data=include_table_data,
    )
    if source_branch:
        metadata["source_branch"] = source_branch
    endpoint = branch_database_endpoint()
    options = branch_pg_options(
        mode,
        schemas,
        no_owner,
        no_privileges,
        include_table_data=include_table_data,
    )
    dump_path = target_dir / branch_dump_filename(mode)
    tmp_dump_path = target_dir / f".{dump_path.name}.{uuid.uuid4().hex}.tmp"
    storage_path = branch_storage_archive_path(name)
    tmp_storage_path = target_dir / f".storage.tar.gz.{uuid.uuid4().hex}.tmp"
    ledger_path = branch_migration_ledger_path(name)
    tmp_ledger_path = target_dir / f".schema_migrations.json.{uuid.uuid4().hex}.tmp"
    stopped = []

    try:
        if mode == "full" and manage_services:
            stopped = stop_running_containers(
                job_id,
                split_config_list(BRANCH_FULL_SERVICE_CONTAINERS),
            )
        elif include_storage_files and manage_services:
            stopped = stop_running_containers(
                job_id,
                split_config_list(BRANCH_STORAGE_SERVICE_CONTAINERS),
            )

        append_job_output(
            job_id,
            (
                f"Creating {mode} "
                f"{'schema/data' if include_table_data else 'schema-only'} "
                f"database snapshot for branch {name}\n"
            ),
        )
        run_logged_command_to_file(
            job_id,
            pg_dump_command(endpoint, options),
            str(tmp_dump_path),
        )
        tmp_dump_path.replace(dump_path)

        if include_storage_files:
            snapshot_storage_files(job_id, tmp_storage_path)
            tmp_storage_path.replace(storage_path)
        else:
            storage_path.unlink(missing_ok=True)

        migrations = snapshot_migration_ledger(job_id, endpoint, tmp_ledger_path)
        tmp_ledger_path.replace(ledger_path)
        metadata["migration_count"] = len(migrations)

        if mode == "full":
            old_app_dump = target_dir / "public.dump"
            if old_app_dump != dump_path:
                old_app_dump.unlink(missing_ok=True)
        else:
            old_full_dump = target_dir / "db.dump"
            if old_full_dump != dump_path:
                old_full_dump.unlink(missing_ok=True)

        write_branch_metadata(name, metadata)
        append_job_output(job_id, f"Snapshot saved for branch {name}\n")
        return metadata
    finally:
        tmp_dump_path.unlink(missing_ok=True)
        tmp_storage_path.unlink(missing_ok=True)
        tmp_ledger_path.unlink(missing_ok=True)
        if stopped:
            start_containers(job_id, stopped)


def restore_branch_state(job_id, name, metadata, manage_services=True):
    mode = metadata.get("mode", "full")
    schemas = metadata.get("schemas") or ["public"]
    if mode == "full":
        schemas = None
    endpoint = branch_database_endpoint()
    options = branch_pg_options(
        mode,
        schemas,
        include_table_data=branch_includes_table_data(metadata),
    )
    dump_path = branch_dump_path(name, mode)
    if not dump_path.exists():
        raise RuntimeError(f"branch dump is missing: {dump_path}")

    stopped = []
    try:
        if mode == "full" and manage_services:
            stopped = stop_running_containers(
                job_id,
                split_config_list(BRANCH_FULL_SERVICE_CONTAINERS),
            )
        elif metadata.get("includes_storage_files") and manage_services:
            stopped = stop_running_containers(
                job_id,
                split_config_list(BRANCH_STORAGE_SERVICE_CONTAINERS),
            )

        append_job_output(job_id, f"Restoring {mode} database snapshot for branch {name}\n")
        clean_restore = True
        if mode == "app-only":
            append_job_output(
                job_id,
                "Dropping resettable app-owned objects before restore\n",
            )
            run_logged_sql(job_id, endpoint, reset_app_schema_objects_sql(schemas))
            clean_restore = False

        run_logged_command(
            job_id,
            pg_restore_command(endpoint, options, clean=clean_restore),
            input_path=str(dump_path),
        )
        restore_migration_ledger(job_id, endpoint, branch_migration_ledger_path(name))

        if metadata.get("includes_storage_files"):
            restore_storage_files(job_id, branch_storage_archive_path(name))
    finally:
        if stopped:
            start_containers(job_id, stopped)

    if mode == "app-only":
        restart_running_containers(
            job_id,
            split_config_list(BRANCH_APP_RESTART_CONTAINERS),
        )
