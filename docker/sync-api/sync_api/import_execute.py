from .settings import *
from .audit import append_job_output, start_job
from .http_utils import parse_bool_body
from .import_plan import (
    build_import_plan,
    import_default_options,
    import_side_config,
    public_import_side,
    side_environment_config,
)
from .operations import reset_database_copy, reset_edge_functions


PLATFORM_TO_LOCAL_CONFIRMATION = "IMPORT PLATFORM TO LOCAL"


def parse_platform_to_local_options(body):
    options = import_default_options(body)
    dry_run = parse_bool_body(body, "dry_run", False)
    if not dry_run and body.get("confirm") != PLATFORM_TO_LOCAL_CONFIRMATION:
        raise ValueError(f'confirm must be exactly "{PLATFORM_TO_LOCAL_CONFIRMATION}"')

    if options["include_storage_objects"]:
        raise ValueError(
            "include_storage_objects is not supported yet. Copy storage objects separately through Storage/S3."
        )
    if options["database_mode"] == "schema-and-data" and not options["include_auth_data"]:
        raise ValueError(
            "include_auth_data=false is not supported for schema-and-data execution yet. "
            "Use schema-only or set include_auth_data=true."
        )

    return {
        **options,
        "dry_run": dry_run,
        "reset_database": parse_bool_body(body, "reset_database", True),
        "reset_edge_functions": parse_bool_body(
            body,
            "reset_edge_functions",
            options["include_edge_functions"],
        ),
        "prune_edge_functions": parse_bool_body(body, "prune_edge_functions", True),
        "drop_target_schemas": parse_bool_body(body, "drop_target_schemas", True),
        "no_owner": parse_bool_body(body, "no_owner", True),
        "no_privileges": parse_bool_body(body, "no_privileges", True),
        "include_table_data": options["database_mode"] == "schema-and-data",
        "schemas": body.get("schemas") or [],
    }


def platform_to_local_config(body):
    source = import_side_config(body, "source")
    target = import_side_config(body, "target")
    if source.get("type") not in (None, "platform"):
        raise ValueError("source.type must be platform")
    if target.get("type") not in (None, "local"):
        raise ValueError("target.type must be local")
    if not source.get("db_url"):
        raise ValueError("source.db_url is required for platform-to-local import")
    if not target.get("db_url") and not target.get("container"):
        target["container"] = "supabase-db"

    config = {
        "name": body.get("name") or body.get("environment") or "platform-to-local",
        "source_env": source.get("label") or "platform",
        "target_env": target.get("label") or "local",
        "sync_storage_buckets": body.get("include_storage_bucket_metadata", True),
        **side_environment_config(source),
        **side_environment_config(target),
    }
    if source.get("edge_functions_dir") is None:
        config.pop("source_edge_functions_dir", None)
    if target.get("edge_functions_dir") is None and target.get("type") == "local":
        config["target_edge_functions_dir"] = str(EDGE_FUNCTIONS_DIR)
    return config, source, target


def platform_to_local_command_summary(source, target, options):
    command = [
        "sync-api-import",
        "platform-to-local",
        "--database-mode",
        options["database_mode"],
    ]
    if options["dry_run"]:
        command.append("--dry-run")
    if options["include_table_data"]:
        command.append("--include-table-data")
    else:
        command.append("--schema-only")
    if options["reset_edge_functions"]:
        command.append("--edge-functions")
    if options["drop_target_schemas"]:
        command.append("--drop-target-schemas")
    command += ["--source", source.get("project_ref") or "platform-db"]
    command += ["--target", target.get("container") or target.get("db_url") or "local-db"]
    return command


def run_platform_to_local_job(job_id, body):
    options = parse_platform_to_local_options(body)
    config, source, target = platform_to_local_config(body)
    plan = build_import_plan(body)

    append_job_output(job_id, "Platform-to-local import started\n")
    append_job_output(
        job_id,
        "Control-plane state remains in sync-api /data; source and target databases are not used as tool metadata stores.\n",
    )
    append_job_output(
        job_id,
        (
            "Storage object bytes are not copied by this job. "
            "Use the future Storage/S3 copy workflow for object files.\n"
        ),
    )
    append_job_output(
        job_id,
        f"Source: {json.dumps(public_import_side(source), sort_keys=True)}\n",
    )
    append_job_output(
        job_id,
        f"Target: {json.dumps(public_import_side(target), sort_keys=True)}\n",
    )

    if options["dry_run"]:
        append_job_output(job_id, "Dry run only; no changes will be written.\n")
        append_job_output(
            job_id,
            json.dumps(
                {
                    "feasibility": plan["feasibility"],
                    "warnings": plan["warnings"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return

    if not plan["source"]["database"].get("available"):
        raise RuntimeError(
            "Source database is not available: "
            + plan["source"]["database"].get("error", "unknown error")
        )
    if not plan["target"]["database"].get("available"):
        raise RuntimeError(
            "Target database is not available: "
            + plan["target"]["database"].get("error", "unknown error")
        )

    if options["reset_database"]:
        reset_database_copy(job_id, config, config["name"], "source", "target", options)
    else:
        append_job_output(job_id, "Database import disabled by reset_database=false\n")

    if options["reset_edge_functions"]:
        reset_edge_functions(job_id, config, config["name"], "source", "target", options)
    else:
        append_job_output(job_id, "Edge function import disabled\n")

    append_job_output(job_id, "Platform-to-local import completed.\n")


def start_platform_to_local_import(body):
    options = parse_platform_to_local_options(body)
    config, source, target = platform_to_local_config(body)
    return start_job(
        "import_platform_to_local",
        config["name"],
        platform_to_local_command_summary(source, target, options),
        runner=lambda job_id: run_platform_to_local_job(job_id, body),
    )
