from .settings import *
from .audit import *
from .branches import *
from .database import *
from .edge_functions import *
from .env_config import *
from .http_utils import *
from .import_execute import *
from .import_plan import *
from .sync_definition import *
from .operations import *

REPAIR_LOG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.log$")


def _safe_repair_log_path(name):
    if name == "latest":
        candidate = REPAIR_LOGS_DIR / "latest.log"
    elif REPAIR_LOG_NAME_RE.match(name):
        candidate = REPAIR_LOGS_DIR / name
    else:
        raise ValueError("Repair log not found")

    root = REPAIR_LOGS_DIR.resolve()
    resolved = candidate.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("Repair log not found")
    if not resolved.exists() or not resolved.is_file():
        raise ValueError("Repair log not found")
    return resolved


def _repair_log_entry(path):
    stat = path.stat()
    return {
        "name": path.name,
        "size_bytes": stat.st_size,
        "modified_at_ms": int(stat.st_mtime * 1000),
        "latest": path.name == "latest.log",
        "url": f"/v1/repair-logs/{path.name}",
    }


def list_repair_logs():
    if not REPAIR_LOGS_DIR.exists():
        return {"logs": [], "latest": None}

    entries = []
    for path in REPAIR_LOGS_DIR.glob("*.log"):
        if not path.is_file() and not path.is_symlink():
            continue
        try:
            entries.append(_repair_log_entry(path.resolve()))
        except FileNotFoundError:
            continue

    entries.sort(key=lambda item: item["modified_at_ms"], reverse=True)
    latest = None
    try:
        latest_path = _safe_repair_log_path("latest")
        latest = _repair_log_entry(latest_path)
        latest["url"] = "/v1/repair-logs/latest"
    except ValueError:
        pass
    return {"logs": entries, "latest": latest}


def read_repair_log(name):
    path = _safe_repair_log_path(name)
    return path.read_text(encoding="utf-8", errors="replace")


class SyncApiHandler(BaseHTTPRequestHandler):
    server_version = "sync-api/0.1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, status, payload):
        self._response_status = status
        self._response_summary = response_summary_for_log(payload)
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_markdown(self, status, body_text):
        self._response_status = status
        self._response_summary = {"markdown_chars": len(body_text)}
        body = body_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status, body_text):
        self._response_status = status
        self._response_summary = {"text_chars": len(body_text)}
        body = body_text.encode("utf-8", errors="replace")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json(status, {"error": message})

    def authenticate(self):
        path = urlparse(self.path).path
        if path == "/v1/repair-logs" or path.startswith("/v1/repair-logs/"):
            return True
        if path in (
            "/health",
            "/v1/jrp-supabase-slim.json",
            "/v1/branching.md",
            "/v1/imports.md",
        ):
            return True
        if not API_TOKEN:
            self.send_error_json(503, "SYNC_API_TOKEN is not configured")
            return False
        expected = f"Bearer {API_TOKEN}"
        actual = self.headers.get("Authorization", "")
        if not secrets.compare_digest(actual, expected):
            self.send_error_json(401, "Unauthorized")
            return False
        return True

    def handle_request_with_logging(self, handler):
        self._request_id = uuid.uuid4().hex
        self._request_started_ms = now_ms()
        self._request_body_for_log = None
        self._response_status = None
        self._response_summary = None
        self._error_for_log = None

        try:
            if not self.authenticate():
                return
            handler()
        except ValueError as exc:
            self._error_for_log = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            self.send_error_json(400, str(exc))
        except Exception as exc:
            self._error_for_log = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
            self.send_error_json(500, str(exc))
        finally:
            self.log_request_event()

    def log_request_event(self):
        parsed = urlparse(self.path)
        duration_ms = now_ms() - getattr(self, "_request_started_ms", now_ms())
        event = {
            "type": "request",
            "request_id": getattr(self, "_request_id", None),
            "method": self.command,
            "path": parsed.path,
            "query": sanitize_query_for_log(parse_qs(parsed.query)),
            "status": getattr(self, "_response_status", None),
            "duration_ms": duration_ms,
            "remote_addr": self.client_address[0] if self.client_address else None,
            "user_agent": self.headers.get("User-Agent"),
        }
        if getattr(self, "_request_body_for_log", None) is not None:
            event["request_body"] = self._request_body_for_log
        if getattr(self, "_response_summary", None) is not None:
            event["response"] = self._response_summary
        if getattr(self, "_error_for_log", None) is not None:
            event["error"] = self._error_for_log
        audit_log(event)

    def do_GET(self):
        self.handle_request_with_logging(self.handle_get)

    def do_POST(self):
        self.handle_request_with_logging(self.handle_post)

    def do_DELETE(self):
        self.handle_request_with_logging(self.handle_delete)

    def handle_get(self):
        path = urlparse(self.path).path
        parts = [part for part in path.split("/") if part]

        if path == "/health":
            self.send_json(200, {"status": "ok"})
            return

        if path == "/v1":
            self.send_json(
                200,
                {
                    "service": "sync-api",
                    "endpoints": [
                        "GET /health",
                        "GET /v1/jrp-supabase-slim.json",
                        "GET /v1/branching.md",
                        "GET /v1/imports.md",
                        "GET /v1/repair-logs",
                        "GET /v1/repair-logs/latest",
                        "GET /v1/repair-logs/{name}",
                        "GET /v1/environments",
                        "POST /v1/environments",
                        "POST /v1/environments/setup",
                        "GET /v1/environments/{name}",
                        "DELETE /v1/environments/{name}",
                        "GET /v1/environments/{name}/identity",
                        "GET /v1/environments/{name}/schemas?role=source|target|both",
                        "GET /v1/environments/{name}/stats",
                        "GET /v1/environments/{name}/table-stats?schema={schema}&table={table}",
                        "GET /v1/environments/{name}/metadata",
                        "GET /v1/environments/{name}/database-functions",
                        "GET /v1/environments/{name}/database-triggers",
                        "GET /v1/environments/{name}/edge-functions",
                        "GET /v1/environments/{name}/edge-functions/{id}",
                        "GET /v1/environments/{name}/edge-functions/{id}/source",
                        "GET /v1/environments/{name}/migrations",
                        "GET /v1/environments/{name}/migrations/{version}",
                        "POST /v1/environments/{name}/validate",
                        "POST /v1/environments/{name}/migrations/plan",
                        "POST /v1/environments/{name}/migrations/up",
                        "POST /v1/environments/{name}/reset/source",
                        "POST /v1/environments/{name}/reset/destination",
                        "GET /v1/supabase/organizations",
                        "GET /v1/supabase/projects",
                        "GET /v1/supabase/projects/{ref}",
                        "GET /v1/supabase/projects/{ref}/backups",
                        "POST /v1/imports/plan",
                        "POST /v1/imports/platform-to-local",
                        "GET /v1/branches",
                        "POST /v1/branches",
                        "GET /v1/branches/schemas",
                        "GET /v1/branches/active",
                        "POST /v1/branches/save",
                        "GET /v1/branches/{name}",
                        "POST /v1/branches/{name}/save",
                        "POST /v1/branches/{name}/switch",
                        "POST /v1/branches/{name}/merge",
                        "POST /v1/branches/{name}/reset",
                        "DELETE /v1/branches/{name}",
                        "GET /v1/jobs",
                        "GET /v1/jobs/{id}",
                    ],
                },
            )
            return

        if path == "/v1/jrp-supabase-slim.json":
            self.send_json(200, read_sync_api_definition())
            return

        if path == "/v1/branching.md":
            self.send_markdown(200, read_branching_doc())
            return

        if path == "/v1/imports.md":
            self.send_markdown(200, read_import_doc())
            return

        if parts == ["v1", "repair-logs"]:
            self.send_json(200, list_repair_logs())
            return

        if len(parts) == 3 and parts[:2] == ["v1", "repair-logs"]:
            try:
                self.send_text(200, read_repair_log(parts[2]))
            except ValueError as exc:
                self.send_error_json(404, str(exc))
            return

        if parts == ["v1", "environments"]:
            data = load_store()
            environments = [
                public_environment(name, config)
                for name, config in sorted(data["environments"].items())
            ]
            self.send_json(200, {"environments": environments})
            return

        if parts == ["v1", "branches"]:
            active_branch = read_active_branch()
            branches = [
                public_branch(metadata, active_branch)
                for metadata in list_branch_metadata()
            ]
            self.send_json(
                200,
                {
                    "active_branch": active_branch,
                    "branches": branches,
                },
            )
            return

        if parts == ["v1", "branches", "schemas"]:
            self.send_json(200, branch_database_schemas())
            return

        if parts == ["v1", "branches", "active"]:
            active_branch = read_active_branch()
            branch = None
            if active_branch:
                try:
                    branch = public_branch(
                        read_branch_metadata(active_branch),
                        active_branch,
                    )
                except ValueError:
                    branch = None
            self.send_json(
                200,
                {
                    "active_branch": active_branch,
                    "branch": branch,
                },
            )
            return

        if len(parts) == 3 and parts[:2] == ["v1", "branches"]:
            name = validate_branch_name(parts[2])
            try:
                metadata = read_branch_metadata(name)
            except ValueError:
                self.send_error_json(404, "Branch not found")
                return
            self.send_json(
                200,
                {
                    "branch": public_branch(metadata, read_active_branch()),
                },
            )
            return

        if len(parts) == 3 and parts[:2] == ["v1", "environments"]:
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            self.send_json(200, {"environment": public_environment(name, config)})
            return

        if len(parts) == 4 and parts[:2] == ["v1", "environments"] and parts[3] == "identity":
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            self.send_json(200, environment_identity(name, config))
            return

        if len(parts) == 4 and parts[:2] == ["v1", "environments"] and parts[3] == "stats":
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            query = parse_qs(urlparse(self.path).query)
            exact_rows = parse_bool_query(query, "exact_rows", False)
            include_columns = parse_bool_query(query, "include_columns", False)
            self.send_json(
                200,
                environment_stats(name, config, exact_rows, include_columns),
            )
            return

        if (
            len(parts) == 4
            and parts[:2] == ["v1", "environments"]
            and parts[3] == "table-stats"
        ):
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            query = parse_qs(urlparse(self.path).query)
            schema_name = parse_string_query(query, "schema", "public")
            table_name = parse_string_query(query, "table")
            exact_rows = parse_bool_query(query, "exact_rows", False)
            self.send_json(
                200,
                environment_table_stats(
                    name, config, schema_name, table_name, exact_rows
                ),
            )
            return

        if len(parts) == 4 and parts[:2] == ["v1", "environments"] and parts[3] == "schemas":
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            query = parse_qs(urlparse(self.path).query)
            self.send_json(200, environment_schemas(name, config, parse_schema_roles(query)))
            return

        if len(parts) in (4, 5) and parts[:2] == ["v1", "environments"] and parts[3] == "migrations":
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            if len(parts) == 4:
                self.send_json(200, environment_migrations(name, config))
                return
            try:
                self.send_json(200, environment_migration_detail(name, config, parts[4]))
            except ValueError:
                self.send_error_json(404, "Migration not found")
            return

        if len(parts) == 4 and parts[:2] == ["v1", "environments"] and parts[3] == "metadata":
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            query = parse_qs(urlparse(self.path).query)
            include_internal = parse_bool_query(query, "include_internal", False)
            self.send_json(200, environment_metadata(name, config, include_internal))
            return

        if (
            len(parts) == 4
            and parts[:2] == ["v1", "environments"]
            and parts[3] == "database-functions"
        ):
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            self.send_json(200, environment_database_functions(name, config))
            return

        if (
            len(parts) == 4
            and parts[:2] == ["v1", "environments"]
            and parts[3] == "database-triggers"
        ):
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            query = parse_qs(urlparse(self.path).query)
            include_internal = parse_bool_query(query, "include_internal", False)
            self.send_json(
                200,
                environment_database_triggers(name, config, include_internal),
            )
            return

        if (
            len(parts) == 4
            and parts[:2] == ["v1", "environments"]
            and parts[3] == "edge-functions"
        ):
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            self.send_json(200, environment_edge_functions(name, config))
            return

        if (
            len(parts) in (5, 6)
            and parts[:2] == ["v1", "environments"]
            and parts[3] == "edge-functions"
            and (len(parts) == 5 or parts[5] == "source")
        ):
            data = load_store()
            name = parts[2]
            config = data["environments"].get(name)
            if not config:
                self.send_error_json(404, "Environment not found")
                return
            include_source = len(parts) == 6
            self.send_json(
                200,
                environment_edge_function(name, config, parts[4], include_source),
            )
            return

        if parts == ["v1", "jobs"]:
            with jobs_lock:
                payload = [
                    {key: value for key, value in job.items() if key != "output"}
                    for job in jobs.values()
                ]
            payload.sort(key=lambda item: item["created_at_ms"], reverse=True)
            self.send_json(200, {"jobs": payload})
            return

        if parts == ["v1", "supabase", "organizations"]:
            self.send_json(200, list_supabase_organizations(self))
            return

        if parts == ["v1", "supabase", "projects"]:
            self.send_json(200, list_supabase_projects(self))
            return

        if len(parts) == 4 and parts[:3] == ["v1", "supabase", "projects"]:
            self.send_json(200, get_supabase_project(self, parts[3]))
            return

        if (
            len(parts) == 5
            and parts[:3] == ["v1", "supabase", "projects"]
            and parts[4] == "backups"
        ):
            self.send_json(200, list_supabase_project_backups(self, parts[3]))
            return

        if len(parts) == 3 and parts[:2] == ["v1", "jobs"]:
            with jobs_lock:
                job = jobs.get(parts[2])
            if not job:
                self.send_error_json(404, "Job not found")
                return
            self.send_json(200, {"job": job})
            return

        self.send_error_json(404, "Not found")

    def handle_post(self):
        path = urlparse(self.path).path
        parts = [part for part in path.split("/") if part]
        body = read_json(self)
        self._request_body_for_log = body

        if parts == ["v1", "environments"]:
            name, config = environment_config_from_create_body(body)

            data = load_store()
            data["environments"][name] = config
            save_store(data)
            self.send_json(201, {"environment": public_environment(name, config)})
            return

        if parts == ["v1", "environments", "setup"]:
            name, config = environment_config_from_setup_body(body)

            data = load_store()
            data["environments"][name] = config
            save_store(data)
            self.send_json(
                201,
                {
                    "environment": public_environment(name, config),
                    "identity": environment_identity(name, config),
                },
            )
            return

        if parts == ["v1", "imports", "plan"]:
            self.send_json(200, build_import_plan(body))
            return

        if parts == ["v1", "imports", "platform-to-local"]:
            job = start_platform_to_local_import(body)
            self.send_json(202, {"job": job})
            return

        if parts == ["v1", "branches"]:
            options = parse_branch_create_options(body)
            job = start_job(
                "branch_create",
                options["name"],
                branch_create_command(options),
                runner=lambda job_id: run_create_branch_job(job_id, options),
            )
            self.send_json(202, {"job": job})
            return

        if parts == ["v1", "branches", "save"]:
            active_branch = read_active_branch()
            if not active_branch:
                self.send_error_json(400, "No active branch is set")
                return
            job = start_job(
                "branch_save",
                active_branch,
                ["supabranch", "save"],
                runner=lambda job_id: run_save_branch_job(job_id, active_branch, body),
            )
            self.send_json(202, {"job": job})
            return

        if (
            len(parts) == 4
            and parts[:2] == ["v1", "branches"]
            and parts[3] in ("save", "switch", "merge", "reset")
        ):
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            branch_name = validate_branch_name(parts[2])
            action = parts[3]
            if action == "save":
                command = ["supabranch", "save", branch_name]
                runner = lambda job_id: run_save_branch_job(job_id, branch_name, body)
                kind = "branch_save"
            elif action == "switch":
                command = ["supabranch", "switch", branch_name]
                if body.get("autosave", True) is not False:
                    command.append("--autosave")
                runner = lambda job_id: run_switch_branch_job(job_id, branch_name, body)
                kind = "branch_switch"
            elif action == "merge":
                target = body.get("target_branch") or body.get("target") or BRANCH_DEFAULT_BASELINE_NAME
                command = ["supabranch", "merge", branch_name, "--into", str(target)]
                if body.get("autosave", True) is not False:
                    command.append("--autosave")
                if body.get("activate", True) is not False:
                    command.append("--activate")
                runner = lambda job_id: run_merge_branch_job(job_id, branch_name, body)
                kind = "branch_merge"
            else:
                source = body.get("from") or body.get("source_branch") or ""
                command = ["supabranch", "reset", branch_name, "--from", source]
                runner = lambda job_id: run_reset_branch_job(job_id, branch_name, body)
                kind = "branch_reset"
            job = start_job(kind, branch_name, command, runner=runner)
            self.send_json(202, {"job": job})
            return

        if len(parts) == 4 and parts[:2] == ["v1", "environments"]:
            env_name = parts[2]
            action = parts[3]
            if action not in ("validate",):
                self.send_error_json(404, "Not found")
                return
            self.start_environment_job(env_name, action, body)
            return

        if (
            len(parts) == 5
            and parts[:2] == ["v1", "environments"]
            and parts[3] == "migrations"
            and parts[4] in ("plan", "up")
        ):
            self.start_environment_job(parts[2], parts[4], body)
            return

        if (
            len(parts) == 5
            and parts[:2] == ["v1", "environments"]
            and parts[3] == "reset"
            and parts[4] in ("source", "destination", "target")
        ):
            self.start_reset_job(parts[2], parts[4], body)
            return

        self.send_error_json(404, "Not found")

    def start_environment_job(self, env_name, action, body):
        data = load_store()
        config = data["environments"].get(env_name)
        if not config:
            self.send_error_json(404, "Environment not found")
            return
        if action == "up":
            validate_environment_up_branch(env_name, config)
        command = command_for_environment(config, action, body)
        job = start_job(action, env_name, command)
        self.send_json(202, {"job": job})

    def start_reset_job(self, env_name, side, body):
        data = load_store()
        config = data["environments"].get(env_name)
        if not config:
            self.send_error_json(404, "Environment not found")
            return

        target_role = "source" if side == "source" else "target"
        job_kind = "reset_source" if target_role == "source" else "reset_destination"
        options = parse_reset_options(body, target_role)
        command = reset_command_summary(config, env_name, target_role, options)
        job = start_job(
            job_kind,
            env_name,
            command,
            runner=lambda job_id: run_reset_job(
                job_id,
                env_name,
                config,
                target_role,
                options,
            ),
        )
        self.send_json(202, {"job": job})

    def handle_delete(self):
        path = urlparse(self.path).path
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[:2] == ["v1", "environments"]:
            data = load_store()
            name = parts[2]
            if name not in data["environments"]:
                self.send_error_json(404, "Environment not found")
                return
            removed = data["environments"].pop(name)
            save_store(data)
            self.send_json(200, {"environment": public_environment(name, removed)})
            return
        if len(parts) == 3 and parts[:2] == ["v1", "branches"]:
            name = validate_branch_name(parts[2])
            if read_active_branch() == name:
                self.send_error_json(400, "Cannot delete the active branch")
                return
            target_dir = branch_dir(name)
            if not target_dir.exists() or not (target_dir / "metadata.json").exists():
                self.send_error_json(404, "Branch not found")
                return
            metadata = read_branch_metadata(name)
            shutil.rmtree(target_dir)
            self.send_json(200, {"branch": public_branch(metadata, read_active_branch())})
            return
        self.send_error_json(404, "Not found")


def main():
    if not API_TOKEN:
        print("WARNING: SYNC_API_TOKEN is not configured; protected endpoints will return 503")
    ensure_store()
    server = ThreadingHTTPServer((HOST, PORT), SyncApiHandler)

    def stop_server(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(f"sync-api listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()
