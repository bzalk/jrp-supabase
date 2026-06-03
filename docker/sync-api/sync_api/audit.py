from .settings import *

def now_ms():
    return int(time.time() * 1000)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def is_secret_log_key(key):
    key_lower = str(key).lower()
    return any(part in key_lower for part in SECRET_LOG_KEY_PARTS)


def truncate_log_string(value):
    if len(value) <= LOG_BODY_MAX_CHARS:
        return value
    return value[:LOG_BODY_MAX_CHARS] + "...[truncated]"


def sanitize_for_log(value, key=None):
    if key is not None and is_secret_log_key(key):
        if isinstance(value, str) and "://" in value:
            return mask_url(value)
        return "***" if value not in (None, "") else value

    if isinstance(value, dict):
        return {
            str(item_key): sanitize_for_log(item_value, item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value[:100]]
    if isinstance(value, tuple):
        return [sanitize_for_log(item) for item in value[:100]]
    if isinstance(value, str):
        if "://" in value:
            return mask_url(value)
        return truncate_log_string(value)
    return value


def sanitize_query_for_log(query):
    return {
        key: sanitize_for_log(values[-1] if len(values) == 1 else values, key)
        for key, values in query.items()
    }


def audit_log(event):
    payload = {
        "timestamp": now_iso(),
        "service": "sync-api",
        **event,
    }
    line = json.dumps(sanitize_for_log(payload), sort_keys=True)
    with log_lock:
        print(line, flush=True)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")
        except Exception as exc:
            print(f"sync-api log write failed: {exc}", flush=True)


def ensure_store():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not ENVIRONMENTS_FILE.exists():
        ENVIRONMENTS_FILE.write_text(json.dumps({"environments": {}}, indent=2) + "\n")


def load_store():
    ensure_store()
    with store_lock:
        return json.loads(ENVIRONMENTS_FILE.read_text())


def save_store(data):
    ensure_store()
    tmp_file = ENVIRONMENTS_FILE.with_suffix(".json.tmp")
    with store_lock:
        tmp_file.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp_file.replace(ENVIRONMENTS_FILE)


def mask_url(value):
    if not value:
        return value
    return re.sub(r"://([^:/?#]+):([^@/?#]+)@", r"://\1:***@", value)


def mask_secret(value):
    if value:
        return "***"
    return value


def public_environment(name, config):
    from .env_config import environment_is_protected

    public = dict(config)
    public["name"] = name
    public["protected"] = environment_is_protected(name, config)
    for key in ("source_db_url", "target_db_url"):
        if key in public:
            public[key] = mask_url(public[key])
    for key in SECRET_CONFIG_KEYS:
        if key in public:
            public[key] = mask_secret(public[key])
    return public

def start_job(kind, env_name, command, runner=None):
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "kind": kind,
        "environment": env_name,
        "status": "queued",
        "exit_code": None,
        "created_at_ms": now_ms(),
        "started_at_ms": None,
        "finished_at_ms": None,
        "command": redact_command(command),
        "output": "",
        "progress": {
            "phase": "queued",
            "percent": 0,
            "message": "Queued",
            "updated_at_ms": now_ms(),
            "details": {},
        },
    }

    with jobs_lock:
        jobs[job_id] = job

    audit_log(
        {
            "type": "job",
            "event": "queued",
            "job_id": job_id,
            "job_kind": kind,
            "environment": env_name,
            "command": job["command"],
        }
    )

    if runner is None:
        thread = threading.Thread(target=run_job, args=(job_id, command), daemon=True)
    else:
        thread = threading.Thread(
            target=run_callable_job,
            args=(job_id, runner),
            daemon=True,
        )
    thread.start()
    return job


def redact_command(command):
    redacted = []
    redact_next = False
    for part in command:
        if redact_next:
            redacted.append(mask_url(part))
            redact_next = False
            continue
        redacted.append(mask_url(part) if isinstance(part, str) and "://" in part else part)
        if part in ("--source-db-url", "--target-db-url"):
            redact_next = True
    return redacted

def append_command_output(job_id, command):
    append_job_output(job_id, "$ " + " ".join(redact_command(command)) + "\n")


def run_logged_command(job_id, command, input_path=None):
    append_command_output(job_id, command)
    input_file = None
    try:
        if input_path:
            input_file = open(input_path, "rb")
        process = subprocess.Popen(
            command,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_job_output(job_id, line)
        exit_code = process.wait()
    finally:
        if input_file:
            input_file.close()

    if exit_code != 0:
        raise RuntimeError(f"command failed with exit code {exit_code}")


def run_logged_command_to_file(job_id, command, output_path):
    append_command_output(job_id, command)
    with open(output_path, "wb") as output_file:
        process = subprocess.Popen(
            command,
            stdout=output_file,
            stderr=subprocess.PIPE,
        )
        assert process.stderr is not None
        for raw_line in process.stderr:
            append_job_output(job_id, raw_line.decode("utf-8", errors="replace"))
        exit_code = process.wait()

    if exit_code != 0:
        raise RuntimeError(f"command failed with exit code {exit_code}")


def run_logged_sql(job_id, endpoint, sql):
    command = psql_command(endpoint)
    append_command_output(job_id, command)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stdout, _ = process.communicate(sql)
    if stdout:
        append_job_output(job_id, stdout)
    if process.returncode != 0:
        raise RuntimeError(f"psql failed with exit code {process.returncode}")

def append_job_output(job_id, text):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["output"] += text
        if len(job["output"]) > 200_000:
            job["output"] = job["output"][-200_000:]


def update_job_progress(job_id, phase, percent, message=None, details=None):
    percent = max(0, min(100, int(percent)))
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        current = job.get("progress") or {}
        merged_details = dict(current.get("details") or {})
        if details:
            merged_details.update(details)
        job["progress"] = {
            "phase": phase,
            "percent": percent,
            "message": message or phase.replace("_", " ").title(),
            "updated_at_ms": now_ms(),
            "details": merged_details,
        }


def run_job(job_id, command):
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["started_at_ms"] = now_ms()
        job = dict(jobs[job_id])
    update_job_progress(job_id, "running", 5, "Job started")

    audit_log(
        {
            "type": "job",
            "event": "started",
            "job_id": job_id,
            "job_kind": job.get("kind"),
            "environment": job.get("environment"),
            "command": job.get("command"),
        }
    )

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_job_output(job_id, line)
        exit_code = process.wait()
        with jobs_lock:
            jobs[job_id]["exit_code"] = exit_code
            jobs[job_id]["status"] = "succeeded" if exit_code == 0 else "failed"
            jobs[job_id]["finished_at_ms"] = now_ms()
            job = dict(jobs[job_id])
        update_job_progress(
            job_id,
            "succeeded" if exit_code == 0 else "failed",
            100 if exit_code == 0 else 95,
            "Job completed" if exit_code == 0 else "Job failed",
        )
        audit_log(
            {
                "type": "job",
                "event": "finished",
                "job_id": job_id,
                "job_kind": job.get("kind"),
                "environment": job.get("environment"),
                "status": job.get("status"),
                "exit_code": exit_code,
                "duration_ms": (
                    job["finished_at_ms"] - job["started_at_ms"]
                    if job.get("started_at_ms") and job.get("finished_at_ms")
                    else None
                ),
            }
        )
    except Exception as exc:
        append_job_output(job_id, f"Job runner error: {exc}\n")
        with jobs_lock:
            jobs[job_id]["exit_code"] = 1
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["finished_at_ms"] = now_ms()
            job = dict(jobs[job_id])
        update_job_progress(job_id, "failed", 95, "Job failed")
        audit_log(
            {
                "type": "job",
                "event": "failed",
                "job_id": job_id,
                "job_kind": job.get("kind"),
                "environment": job.get("environment"),
                "status": "failed",
                "exit_code": 1,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            }
        )


def run_callable_job(job_id, runner):
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["started_at_ms"] = now_ms()
        job = dict(jobs[job_id])
    update_job_progress(job_id, "running", 5, "Job started")

    audit_log(
        {
            "type": "job",
            "event": "started",
            "job_id": job_id,
            "job_kind": job.get("kind"),
            "environment": job.get("environment"),
            "command": job.get("command"),
        }
    )

    try:
        runner(job_id)
        with jobs_lock:
            jobs[job_id]["exit_code"] = 0
            jobs[job_id]["status"] = "succeeded"
            jobs[job_id]["finished_at_ms"] = now_ms()
            job = dict(jobs[job_id])
        update_job_progress(job_id, "succeeded", 100, "Job completed")
        audit_log(
            {
                "type": "job",
                "event": "finished",
                "job_id": job_id,
                "job_kind": job.get("kind"),
                "environment": job.get("environment"),
                "status": "succeeded",
                "exit_code": 0,
                "duration_ms": (
                    job["finished_at_ms"] - job["started_at_ms"]
                    if job.get("started_at_ms") and job.get("finished_at_ms")
                    else None
                ),
            }
        )
    except Exception as exc:
        append_job_output(job_id, f"Job runner error: {exc}\n")
        with jobs_lock:
            jobs[job_id]["exit_code"] = 1
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["finished_at_ms"] = now_ms()
            job = dict(jobs[job_id])
        update_job_progress(job_id, "failed", 95, "Job failed")
        audit_log(
            {
                "type": "job",
                "event": "failed",
                "job_id": job_id,
                "job_kind": job.get("kind"),
                "environment": job.get("environment"),
                "status": "failed",
                "exit_code": 1,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            }
        )


def response_summary_for_log(payload):
    if not isinstance(payload, dict):
        return None

    if "error" in payload:
        return {"error": truncate_log_string(str(payload["error"]))}

    job = payload.get("job")
    if isinstance(job, dict):
        return {
            "job_id": job.get("id"),
            "job_kind": job.get("kind"),
            "job_status": job.get("status"),
            "environment": job.get("environment"),
            "exit_code": job.get("exit_code"),
        }

    environment = payload.get("environment")
    if isinstance(environment, dict):
        return {"environment": environment.get("name")}
    if isinstance(payload.get("environments"), list):
        return {"environment_count": len(payload["environments"])}
    branch = payload.get("branch")
    if isinstance(branch, dict):
        return {"branch": branch.get("name"), "branch_mode": branch.get("mode")}
    if isinstance(payload.get("branches"), list):
        return {
            "branch_count": len(payload["branches"]),
            "active_branch": payload.get("active_branch"),
        }
    if payload.get("service"):
        return {"service": payload.get("service")}
    return None
