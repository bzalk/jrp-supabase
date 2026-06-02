from .settings import *
from .audit import *
from .http_utils import parse_bool_body
from .branch_core import *
from .branch_snapshot import *

class BranchMaintenanceLock:
    def __init__(self, job_id, operation, branch_name):
        self.job_id = job_id
        self.operation = operation
        self.branch_name = branch_name

    def __enter__(self):
        if not branch_operation_lock.acquire(blocking=False):
            raise RuntimeError("another branch operation is already running")
        try:
            payload = {
                "job_id": self.job_id,
                "operation": self.operation,
                "branch": self.branch_name,
                "created_at": now_iso(),
            }
            write_json_atomic(BRANCH_LOCK_FILE, payload)
        except Exception:
            branch_operation_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        BRANCH_LOCK_FILE.unlink(missing_ok=True)
        branch_operation_lock.release()


def parse_branch_create_options(body):
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")
    name = validate_branch_name(body.get("name"))
    mode = parse_branch_mode(body)
    include_table_data = parse_branch_include_table_data(body, default=True)
    include_storage = parse_bool_body(
        body,
        "include_storage_files",
        mode == "full" and include_table_data,
    )
    if include_storage and not include_table_data:
        raise ValueError("include_storage_files cannot be true when include_table_data is false")
    schemas = ["*"] if mode == "full" else validate_schema_names(
        body.get("schemas"),
        default=split_config_list(BRANCH_DEFAULT_APP_SCHEMAS) or ["public"],
    )
    return {
        "name": name,
        "mode": mode,
        "include_table_data": include_table_data,
        "include_storage_files": include_storage,
        "schemas": schemas,
        "notes": body.get("notes"),
        "overwrite": parse_bool_body(body, "overwrite", False),
        "activate": parse_bool_body(body, "activate", True),
        "no_owner": parse_bool_body(body, "no_owner", True),
        "no_privileges": parse_bool_body(body, "no_privileges", False),
    }


def parse_branch_save_overrides(body, metadata):
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")
    mode = metadata.get("mode", "full")
    include_table_data = parse_branch_include_table_data(body, default=True)
    include_storage = metadata.get("includes_storage_files", mode == "full")
    if "include_storage_files" in body:
        include_storage = parse_bool_body(body, "include_storage_files", include_storage)
    if include_storage and not include_table_data:
        raise ValueError("include_storage_files cannot be true when include_table_data is false")
    schemas = metadata.get("schemas") or (["*"] if mode == "full" else ["public"])
    if mode == "app-only" and "schemas" in body:
        schemas = validate_schema_names(body.get("schemas"), default=["public"])
    return {
        "mode": mode,
        "include_table_data": include_table_data,
        "include_storage_files": include_storage,
        "schemas": schemas,
        "notes": body.get("notes", metadata.get("notes")),
        "no_owner": parse_bool_body(body, "no_owner", True),
        "no_privileges": parse_bool_body(body, "no_privileges", False),
    }


def branch_create_command(options):
    command = ["supabranch", "create", options["name"], f"--{options['mode']}"]
    if not options.get("include_table_data", True):
        command.append("--schema-only")
    if options["include_storage_files"]:
        command.append("--include-storage")
    else:
        command.append("--no-storage")
    if options["mode"] == "app-only":
        command += ["--schemas", ",".join(options["schemas"])]
    if not options.get("activate", True):
        command.append("--no-activate")
    return command


def ensure_initial_baseline_branch(job_id, options):
    baseline_name = validate_branch_name(BRANCH_DEFAULT_BASELINE_NAME)
    if options["name"] == baseline_name or branch_exists(baseline_name):
        return baseline_name if branch_exists(baseline_name) else None

    append_job_output(
        job_id,
        (
            f"No active branch is set; saving current runtime as "
            f"{baseline_name} before creating {options['name']}\n"
        ),
    )
    snapshot_branch_state(
        job_id,
        baseline_name,
        options["mode"],
        options["include_storage_files"],
        options["schemas"],
        notes=f"Baseline before creating {options['name']}",
        overwrite=False,
        no_owner=options.get("no_owner", True),
        no_privileges=options.get("no_privileges", True),
        include_table_data=True,
    )
    write_active_branch(baseline_name)
    append_job_output(job_id, f"Active branch initialized to {baseline_name}\n")
    return baseline_name


def run_create_branch_job(job_id, options):
    name = options["name"]
    with BranchMaintenanceLock(job_id, "create", name):
        source_branch = read_active_branch()
        if source_branch is None:
            source_branch = ensure_initial_baseline_branch(job_id, options)
        metadata = snapshot_branch_state(
            job_id,
            name,
            options["mode"],
            options["include_storage_files"],
            options["schemas"],
            notes=options.get("notes"),
            overwrite=options.get("overwrite", False),
            no_owner=options.get("no_owner", True),
            no_privileges=options.get("no_privileges", True),
            source_branch=source_branch,
            include_table_data=options.get("include_table_data", True),
        )
        if options.get("activate", True):
            if not options.get("include_table_data", True):
                append_job_output(
                    job_id,
                    f"Restoring schema-only branch {name} so the active runtime has empty tables\n",
                )
                restore_branch_state(job_id, name, metadata)
            write_active_branch(name)
            append_job_output(job_id, f"Active branch set to {name}\n")
        elif read_active_branch() is None and source_branch:
            write_active_branch(source_branch)
            append_job_output(job_id, f"Active branch remains {source_branch}\n")
        elif read_active_branch() is None:
            append_job_output(job_id, "No active branch is set\n")
        return metadata


def run_save_branch_job(job_id, name, body):
    with BranchMaintenanceLock(job_id, "save", name):
        metadata = read_branch_metadata(name)
        options = parse_branch_save_overrides(body, metadata)
        snapshot_branch_state(
            job_id,
            name,
            options["mode"],
            options["include_storage_files"],
            options["schemas"],
            notes=options.get("notes"),
            existing_metadata=metadata,
            overwrite=True,
            no_owner=options.get("no_owner", True),
            no_privileges=options.get("no_privileges", True),
            include_table_data=options.get("include_table_data", True),
        )


def run_switch_branch_job(job_id, name, body):
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")
    autosave = parse_bool_body(body, "autosave", True)
    with BranchMaintenanceLock(job_id, "switch", name):
        target_metadata = read_branch_metadata(name)
        current = read_active_branch()
        if current == name and autosave:
            append_job_output(
                job_id,
                f"Branch {name} is already active; no restore was performed\n",
            )
            return
        if autosave and current and current != name:
            try:
                current_metadata = read_branch_metadata(current)
            except ValueError:
                append_job_output(
                    job_id,
                    f"Active branch {current} has no snapshot; skipping autosave\n",
                )
            else:
                append_job_output(job_id, f"Autosaving active branch {current}\n")
                current_options = parse_branch_save_overrides({}, current_metadata)
                snapshot_branch_state(
                    job_id,
                    current,
                    current_options["mode"],
                    current_options["include_storage_files"],
                    current_options["schemas"],
                    notes=current_options.get("notes"),
                    existing_metadata=current_metadata,
                    overwrite=True,
                    include_table_data=current_options.get("include_table_data", True),
                )

        restore_branch_state(job_id, name, target_metadata)
        write_active_branch(name)
        append_job_output(job_id, f"Active branch switched to {name}\n")


def run_reset_branch_job(job_id, name, body):
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")
    source = validate_branch_name(body.get("from") or body.get("source_branch"))
    if source == name:
        raise ValueError("source branch and target branch must be different")
    with BranchMaintenanceLock(job_id, "reset", name):
        source_dir = branch_dir(source)
        target_dir = branch_dir(name)
        if not source_dir.exists() or not (source_dir / "metadata.json").exists():
            raise ValueError("Source branch not found")
        if not target_dir.exists() or not (target_dir / "metadata.json").exists():
            raise ValueError("Target branch not found")

        source_metadata = read_branch_metadata(source)
        target_metadata = read_branch_metadata(name)
        tmp_dir = BRANCHES_DIR / f".{name}.reset.{uuid.uuid4().hex}.tmp"
        shutil.copytree(source_dir, tmp_dir)
        metadata = dict(source_metadata)
        metadata["name"] = name
        metadata["created_at"] = target_metadata.get("created_at", now_iso())
        metadata["updated_at"] = now_iso()
        metadata["reset_from"] = source
        write_json_atomic(tmp_dir / "metadata.json", metadata)
        shutil.rmtree(target_dir)
        tmp_dir.replace(target_dir)
        append_job_output(job_id, f"Branch {name} reset from {source}\n")


def parse_branch_merge_options(source, body):
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")
    target = validate_branch_name(
        body.get("target_branch")
        or body.get("target")
        or BRANCH_DEFAULT_BASELINE_NAME
    )
    main_branch = validate_branch_name(BRANCH_DEFAULT_BASELINE_NAME)
    if target != main_branch:
        raise ValueError(f"Only merges into {main_branch} are supported")
    if source == target:
        raise ValueError("source branch and target branch must be different")
    return {
        "target": target,
        "autosave": parse_bool_body(body, "autosave", True),
        "activate": parse_bool_body(body, "activate", True),
        "notes": body.get("notes"),
        "no_owner": parse_bool_body(body, "no_owner", True),
        "no_privileges": parse_bool_body(body, "no_privileges", False),
    }


def run_merge_branch_job(job_id, source, body):
    options = parse_branch_merge_options(source, body)
    target = options["target"]
    with BranchMaintenanceLock(job_id, "merge", source):
        source_dir = branch_dir(source)
        target_dir = branch_dir(target)
        if not source_dir.exists() or not (source_dir / "metadata.json").exists():
            raise ValueError("Source branch not found")
        if not target_dir.exists() or not (target_dir / "metadata.json").exists():
            raise ValueError("Target branch not found")

        active_branch = read_active_branch()
        source_runtime_matches_snapshot = False
        if options["autosave"] and active_branch == source:
            append_job_output(job_id, f"Autosaving source branch {source}\n")
            source_metadata = read_branch_metadata(source)
            current_options = parse_branch_save_overrides({}, source_metadata)
            snapshot_branch_state(
                job_id,
                source,
                current_options["mode"],
                current_options["include_storage_files"],
                current_options["schemas"],
                notes=current_options.get("notes"),
                existing_metadata=source_metadata,
                overwrite=True,
                no_owner=options["no_owner"],
                no_privileges=options["no_privileges"],
                include_table_data=current_options.get("include_table_data", True),
            )
            source_runtime_matches_snapshot = True

        if active_branch == target and not options["activate"]:
            raise ValueError(
                "Target branch is active; use activate true so the runtime matches the merged snapshot"
            )

        source_metadata = read_branch_metadata(source)
        target_metadata = read_branch_metadata(target)
        tmp_dir = BRANCHES_DIR / f".{target}.merge.{uuid.uuid4().hex}.tmp"
        shutil.copytree(source_dir, tmp_dir)
        now = now_iso()
        metadata = dict(source_metadata)
        metadata["name"] = target
        metadata["created_at"] = target_metadata.get("created_at", now)
        metadata["updated_at"] = now
        metadata["merged_from"] = source
        metadata["merged_at"] = now
        metadata["notes"] = options["notes"] or f"Merged {source} into {target}"
        metadata.pop("source_branch", None)
        metadata.pop("reset_from", None)
        write_json_atomic(tmp_dir / "metadata.json", metadata)

        shutil.rmtree(target_dir)
        tmp_dir.replace(target_dir)
        append_job_output(job_id, f"Merged branch {source} into {target}\n")

        if options["activate"]:
            if source_runtime_matches_snapshot:
                append_job_output(
                    job_id,
                    (
                        f"Runtime already matches {source}; marking "
                        f"{target} active after merge\n"
                    ),
                )
            else:
                append_job_output(job_id, f"Restoring merged branch {target}\n")
                restore_branch_state(job_id, target, metadata)
            write_active_branch(target)
            append_job_output(job_id, f"Active branch switched to {target}\n")
