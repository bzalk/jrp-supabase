from .settings import *
from .audit import *
from .db import migration_ledger_export_sql, migration_ledger_restore_sql, psql_json
from .branch_core import *

def docker_container_running(name):
    process = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        return None
    return process.stdout.strip().lower() == "true"


def stop_running_containers(job_id, names):
    stopped = []
    for name in names:
        running = docker_container_running(name)
        if running is None:
            append_job_output(job_id, f"Skipping missing container {name}\n")
            continue
        if not running:
            append_job_output(job_id, f"Container {name} is already stopped\n")
            continue
        run_logged_command(job_id, ["docker", "stop", name])
        stopped.append(name)
    return stopped


def start_containers(job_id, names):
    for name in names:
        if docker_container_running(name) is None:
            append_job_output(job_id, f"Skipping missing container {name}\n")
            continue
        run_logged_command(job_id, ["docker", "start", name])


def restart_running_containers(job_id, names):
    for name in names:
        running = docker_container_running(name)
        if running is None:
            append_job_output(job_id, f"Skipping missing container {name}\n")
            continue
        if not running:
            append_job_output(job_id, f"Container {name} is stopped; skipping restart\n")
            continue
        run_logged_command(job_id, ["docker", "restart", name])


def safe_extract_tar(archive, destination):
    base = destination.resolve()
    for member in archive.getmembers():
        if member.issym() or member.islnk():
            raise RuntimeError("storage archive must not contain links")
        target = (destination / member.name).resolve()
        if target != base and base not in target.parents:
            raise RuntimeError("storage archive contains a path outside the storage directory")
    archive.extractall(destination, filter="data")


def snapshot_storage_files(job_id, output_path):
    if not BRANCH_STORAGE_DIR.exists() or not BRANCH_STORAGE_DIR.is_dir():
        raise RuntimeError(
            f"storage directory is not available at {BRANCH_STORAGE_DIR}; "
            "mount it or set SYNC_API_BRANCH_STORAGE_DIR"
        )
    append_job_output(job_id, f"Archiving storage files from {BRANCH_STORAGE_DIR}\n")
    with tarfile.open(output_path, "w:gz") as archive:
        for child in sorted(BRANCH_STORAGE_DIR.iterdir(), key=lambda path: path.name):
            archive.add(child, arcname=child.name, recursive=True)


def restore_storage_files(job_id, archive_path):
    if not archive_path.exists():
        raise RuntimeError("branch metadata includes storage files but storage.tar.gz is missing")
    BRANCH_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    append_job_output(job_id, f"Replacing storage files in {BRANCH_STORAGE_DIR}\n")
    for child in BRANCH_STORAGE_DIR.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    with tarfile.open(archive_path, "r:gz") as archive:
        safe_extract_tar(archive, BRANCH_STORAGE_DIR)


def snapshot_migration_ledger(job_id, endpoint, output_path):
    append_job_output(job_id, "Saving local migration ledger\n")
    migrations = psql_json(endpoint, migration_ledger_export_sql())
    output_path.write_text(json.dumps(migrations, indent=2, sort_keys=True) + "\n")
    return migrations


def restore_migration_ledger(job_id, endpoint, ledger_path):
    if not ledger_path.exists():
        append_job_output(job_id, "Branch has no migration ledger snapshot; skipping ledger restore\n")
        return
    migrations = json.loads(ledger_path.read_text())
    append_job_output(
        job_id,
        f"Restoring local migration ledger with {len(migrations)} migration(s)\n",
    )
    run_logged_sql(job_id, endpoint, migration_ledger_restore_sql(migrations))
