import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


APP_PATH = Path(__file__).with_name("app.py")
sys.path.insert(0, str(APP_PATH.parent))
import app


class EdgeFunctionPackagingTests(unittest.TestCase):
    def test_local_function_zip_uses_archive_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            functions_root = Path(tmpdir)
            function_dir = functions_root / "hello"
            function_dir.mkdir()
            (function_dir / "index.ts").write_text("Deno.serve(() => new Response('ok'))")

            function_item = app.local_edge_function_payload(
                functions_root,
                "hello",
                include_files=True,
                include_content=True,
                max_file_bytes=None,
            )

            archive_path = functions_root / "hello.zip"
            app.write_edge_function_zip(function_item, archive_path)

            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.namelist(), ["index.ts"])

    def test_archive_path_strips_platform_source_prefix(self):
        self.assertEqual(app.edge_function_archive_path("source/index.ts"), "index.ts")
        self.assertEqual(app.edge_function_archive_path("/source/lib/mod.ts"), "lib/mod.ts")

    def test_metadata_path_strips_absolute_platform_source_prefix(self):
        self.assertEqual(
            app.edge_function_metadata_path(
                "/tmp/user_fn_project_uuid_1/source/index.ts"
            ),
            "index.ts",
        )

    def test_summary_hash_normalizes_platform_source_prefix(self):
        local = [{"path": "index.ts", "sha256": "abc"}]
        hosted = [{"path": "source/index.ts", "sha256": "abc"}]

        self.assertEqual(
            app.edge_function_summary_hash(local),
            app.edge_function_summary_hash(hosted),
        )

    def test_edge_function_digest_ignores_platform_bundle_hash(self):
        self.assertIsNone(app.edge_function_digest({"ezbr_sha256": "bundle"}))
        self.assertEqual(app.edge_function_digest({"sha256": "source"}), "source")

    def test_archive_path_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            app.edge_function_archive_path("../index.ts")
        with self.assertRaises(ValueError):
            app.edge_function_archive_path("lib/../index.ts")

    def test_deploy_request_uses_individual_file_parts(self):
        calls = []
        original_request = app.management_api_request

        def fake_request(endpoint, path, accept="application/json", method="GET", body=None, content_type=None):
            calls.append(
                {
                    "path": path,
                    "method": method,
                    "body": body,
                    "content_type": content_type,
                }
            )
            return 200, {}, b"{}"

        app.management_api_request = fake_request
        try:
            app.deploy_remote_edge_function(
                {
                    "api_base": "https://api.supabase.test",
                    "project_ref": "project-ref",
                    "access_token": "token",
                },
                {
                    "slug": "hello",
                    "entrypoint_path": "index.ts",
                    "files": [
                        {
                            "path": "index.ts",
                            "content": "Deno.serve(() => new Response('ok'))",
                        }
                    ],
                },
            )
        finally:
            app.management_api_request = original_request

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertIn("/functions/deploy?slug=hello", calls[0]["path"])
        body = calls[0]["body"]
        self.assertIn(b'name="metadata"', body)
        self.assertIn(b'filename="index.ts"', body)
        self.assertNotIn(b'filename="hello.zip"', body)
        self.assertNotIn(b"PK\x03\x04", body)


class AuditTests(unittest.TestCase):
    def test_run_logged_sql_uses_psql_command(self):
        job_id = "sql-job"
        calls = []
        original_psql_command = app.psql_command
        original_popen = app.subprocess.Popen

        def fake_psql_command(endpoint):
            calls.append(("command", endpoint))
            return ["psql", "fake"]

        class FakeProcess:
            returncode = 0

            def communicate(self, sql):
                calls.append(("sql", sql))
                return ("OK\n", None)

        def fake_popen(command, stdin=None, stdout=None, stderr=None, text=None):
            calls.append(("popen", command, stdin, stdout, stderr, text))
            return FakeProcess()

        with app.jobs_lock:
            app.jobs[job_id] = {"id": job_id, "output": ""}

        app.psql_command = fake_psql_command
        app.subprocess.Popen = fake_popen
        try:
            app.run_logged_sql(job_id, {"kind": "container"}, "select 1;")
            with app.jobs_lock:
                output = app.jobs[job_id]["output"]
        finally:
            app.psql_command = original_psql_command
            app.subprocess.Popen = original_popen
            with app.jobs_lock:
                app.jobs.pop(job_id, None)

        self.assertEqual(calls[0], ("command", {"kind": "container"}))
        self.assertEqual(calls[1][0], "popen")
        self.assertEqual(calls[2], ("sql", "select 1;"))
        self.assertIn("$ psql fake", output)
        self.assertIn("OK", output)


class BranchManagerTests(unittest.TestCase):
    def test_create_options_support_app_only_flag_and_schemas(self):
        options = app.parse_branch_create_options(
            {
                "name": "feature-dashboard",
                "app_only": True,
                "schemas": ["public", "app"],
            }
        )

        self.assertEqual(options["mode"], "app-only")
        self.assertFalse(options["include_storage_files"])
        self.assertEqual(options["schemas"], ["public", "app"])
        self.assertTrue(options["activate"])

    def test_create_options_support_activate_false(self):
        options = app.parse_branch_create_options(
            {
                "name": "snapshot-only",
                "mode": "app-only",
                "activate": False,
            }
        )

        self.assertFalse(options["activate"])

    def test_create_options_support_schema_only_data_mode(self):
        options = app.parse_branch_create_options(
            {
                "name": "empty-cart",
                "mode": "app-only",
                "schemas": ["public"],
                "data_mode": "schema-only",
            }
        )

        self.assertFalse(options["include_table_data"])
        self.assertFalse(options["include_storage_files"])

    def test_schema_only_rejects_storage_files(self):
        with self.assertRaises(ValueError):
            app.parse_branch_create_options(
                {
                    "name": "empty-cart",
                    "mode": "full",
                    "include_table_data": False,
                    "include_storage_files": True,
                }
            )

    def test_save_options_include_table_data_by_default(self):
        metadata = app.branch_snapshot_metadata(
            "empty-cart",
            "app-only",
            False,
            ["public"],
            "empty branch",
            include_table_data=False,
        )

        options = app.parse_branch_save_overrides({}, metadata)

        self.assertTrue(options["include_table_data"])

    def test_create_options_reject_ambiguous_mode(self):
        with self.assertRaises(ValueError):
            app.parse_branch_create_options(
                {
                    "name": "feature-dashboard",
                    "mode": "full",
                    "app_only": True,
                }
            )

    def test_pg_commands_include_app_only_schemas(self):
        endpoint = {"kind": "url", "db_url": "postgres://user:pass@example.test/postgres"}

        dump_command = app.pg_dump_command(
            endpoint,
            {"schemas": ["public", "app"], "no_owner": True, "no_privileges": True},
        )
        restore_command = app.pg_restore_command(
            endpoint,
            {"schemas": ["public", "app"], "no_owner": True, "no_privileges": True},
        )

        self.assertIn("--schema", dump_command)
        self.assertEqual(dump_command.count("--schema"), 2)
        self.assertIn("public", dump_command)
        self.assertIn("app", dump_command)
        self.assertEqual(restore_command.count("--schema"), 2)

    def test_schema_only_pg_dump_uses_schema_only_flag(self):
        endpoint = {"kind": "url", "db_url": "postgres://user:pass@example.test/postgres"}

        dump_command = app.pg_dump_command(
            endpoint,
            {
                "schemas": ["public"],
                "no_owner": True,
                "no_privileges": True,
                "include_table_data": False,
            },
        )

        self.assertIn("--schema-only", dump_command)

    def test_branch_database_endpoint_defaults_to_owner_capable_local_role(self):
        original_db_url = app.BRANCH_DB_URL
        original_user = app.BRANCH_DB_USER
        try:
            app.BRANCH_DB_URL = ""
            app.BRANCH_DB_USER = "supabase_admin"
            endpoint = app.branch_database_endpoint()
        finally:
            app.BRANCH_DB_URL = original_db_url
            app.BRANCH_DB_USER = original_user

        self.assertEqual(endpoint["user"], "supabase_admin")

    def test_schema_only_create_restores_when_activated(self):
        original_snapshot = app.snapshot_branch_state
        original_restore = app.restore_branch_state
        original_read_active = app.read_active_branch
        original_write_active = app.write_active_branch
        original_lock = app.BranchMaintenanceLock
        calls = []

        class NoopLock:
            def __init__(self, job_id, operation, branch_name):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_snapshot(job_id, name, mode, include_storage_files, schemas, **kwargs):
            calls.append(("snapshot", name, kwargs.get("include_table_data")))
            return app.branch_snapshot_metadata(
                name,
                mode,
                include_storage_files,
                schemas,
                kwargs.get("notes"),
                include_table_data=kwargs.get("include_table_data", True),
            )

        def fake_restore(job_id, name, metadata, manage_services=True):
            calls.append(("restore", name, metadata["includes_table_data"]))

        try:
            app.snapshot_branch_state = fake_snapshot
            app.restore_branch_state = fake_restore
            app.read_active_branch = lambda: "main"
            app.write_active_branch = lambda name: calls.append(("active", name))
            app.BranchMaintenanceLock = NoopLock

            app.run_create_branch_job(
                "job-id",
                {
                    "name": "empty-cart",
                    "mode": "app-only",
                    "include_table_data": False,
                    "include_storage_files": False,
                    "schemas": ["public"],
                    "activate": True,
                    "overwrite": False,
                },
            )
        finally:
            app.snapshot_branch_state = original_snapshot
            app.restore_branch_state = original_restore
            app.read_active_branch = original_read_active
            app.write_active_branch = original_write_active
            app.BranchMaintenanceLock = original_lock

        self.assertEqual(calls[0], ("snapshot", "empty-cart", False))
        self.assertEqual(calls[1], ("restore", "empty-cart", False))
        self.assertEqual(calls[2], ("active", "empty-cart"))

    def test_storage_snapshot_round_trip(self):
        original_storage_dir = app.BRANCH_STORAGE_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            storage_dir = tmpdir / "storage"
            storage_dir.mkdir()
            (storage_dir / "bucket").mkdir()
            (storage_dir / "bucket" / "object.txt").write_text("stored")
            archive_path = tmpdir / "storage.tar.gz"

            app.BRANCH_STORAGE_DIR = storage_dir
            try:
                app.snapshot_storage_files("missing-job", archive_path)
                (storage_dir / "bucket" / "object.txt").write_text("changed")
                app.restore_storage_files("missing-job", archive_path)
            finally:
                app.BRANCH_STORAGE_DIR = original_storage_dir

            self.assertEqual((storage_dir / "bucket" / "object.txt").read_text(), "stored")

    def test_public_branch_marks_active_and_sizes_dump(self):
        original_branches_dir = app.BRANCHES_DIR
        original_active_file = app.BRANCH_ACTIVE_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app.BRANCHES_DIR = tmpdir / "branches"
            app.BRANCH_ACTIVE_FILE = tmpdir / ".branches-active"
            try:
                branch_dir = app.branch_dir("main")
                branch_dir.mkdir(parents=True)
                metadata = app.branch_snapshot_metadata(
                    "main",
                    "full",
                    False,
                    ["*"],
                    "test branch",
                )
                app.write_branch_metadata("main", metadata)
                (branch_dir / "db.dump").write_bytes(b"dump")
                app.write_active_branch("main")

                public = app.public_branch(app.read_branch_metadata("main"), app.read_active_branch())
            finally:
                app.BRANCHES_DIR = original_branches_dir
                app.BRANCH_ACTIVE_FILE = original_active_file

            self.assertTrue(public["active"])
            self.assertEqual(public["dump_size_bytes"], 4)

    def test_switch_active_branch_with_autosave_is_noop(self):
        original_branches_dir = app.BRANCHES_DIR
        original_active_file = app.BRANCH_ACTIVE_FILE
        original_lock_file = app.BRANCH_LOCK_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app.BRANCHES_DIR = tmpdir / "branches"
            app.BRANCH_ACTIVE_FILE = tmpdir / ".branches-active"
            app.BRANCH_LOCK_FILE = tmpdir / ".branches-lock"
            try:
                app.branch_dir("main").mkdir(parents=True)
                metadata = app.branch_snapshot_metadata(
                    "main",
                    "full",
                    False,
                    ["*"],
                    "test branch",
                )
                app.write_branch_metadata("main", metadata)
                app.write_active_branch("main")

                app.run_switch_branch_job("missing-job", "main", {})
            finally:
                app.BRANCHES_DIR = original_branches_dir
                app.BRANCH_ACTIVE_FILE = original_active_file
                app.BRANCH_LOCK_FILE = original_lock_file

            self.assertEqual((tmpdir / ".branches-active").read_text().strip(), "main")

    def test_protected_environment_up_requires_main_branch(self):
        original_active_file = app.BRANCH_ACTIVE_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            app.BRANCH_ACTIVE_FILE = Path(tmpdir) / ".branches-active"
            try:
                app.write_active_branch("feature-cart")
                with self.assertRaises(ValueError):
                    app.validate_environment_up_branch(
                        "production",
                        {"target_env": "production"},
                    )

                app.write_active_branch("main")
                app.validate_environment_up_branch(
                    "production",
                    {"target_env": "production"},
                )
                app.write_active_branch("feature-cart")
                app.validate_environment_up_branch(
                    "hosted-test",
                    {"target_env": "stage", "protected": False},
                )
            finally:
                app.BRANCH_ACTIVE_FILE = original_active_file

    def test_environment_config_defaults_protection_for_production(self):
        production_name, production_config = app.environment_config_from_create_body(
            {
                "name": "production",
                "target_db_url": "postgres://user:pass@example.test/postgres",
            }
        )
        stage_name, stage_config = app.environment_config_from_create_body(
            {
                "name": "stage",
                "target_db_url": "postgres://user:pass@example.test/postgres",
            }
        )
        override_name, override_config = app.environment_config_from_create_body(
            {
                "name": "production",
                "target_db_url": "postgres://user:pass@example.test/postgres",
                "protected": False,
            }
        )

        self.assertEqual(production_name, "production")
        self.assertTrue(production_config["protected"])
        self.assertEqual(stage_name, "stage")
        self.assertFalse(stage_config["protected"])
        self.assertEqual(override_name, "production")
        self.assertFalse(override_config["protected"])
        self.assertTrue(
            app.public_environment(
                "production",
                {"target_env": "production"},
            )["protected"]
        )

    def test_merge_branch_copies_snapshot_into_main(self):
        original_branches_dir = app.BRANCHES_DIR
        original_active_file = app.BRANCH_ACTIVE_FILE
        original_lock_file = app.BRANCH_LOCK_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app.BRANCHES_DIR = tmpdir / "branches"
            app.BRANCH_ACTIVE_FILE = tmpdir / ".branches-active"
            app.BRANCH_LOCK_FILE = tmpdir / ".branches-lock"
            try:
                main_dir = app.branch_dir("main")
                feature_dir = app.branch_dir("feature-cart")
                main_dir.mkdir(parents=True)
                feature_dir.mkdir(parents=True)

                main_metadata = app.branch_snapshot_metadata(
                    "main",
                    "app-only",
                    False,
                    ["public"],
                    "Main",
                )
                feature_metadata = app.branch_snapshot_metadata(
                    "feature-cart",
                    "app-only",
                    False,
                    ["public"],
                    "Cart",
                )
                feature_metadata["source_branch"] = "main"
                app.write_branch_metadata("main", main_metadata)
                app.write_branch_metadata("feature-cart", feature_metadata)
                (main_dir / "public.dump").write_bytes(b"main")
                (feature_dir / "public.dump").write_bytes(b"feature")
                (main_dir / "schema_migrations.json").write_text("[]\n")
                (feature_dir / "schema_migrations.json").write_text(
                    '[{"version":"20260529204637","statements":["select 1;"]}]\n'
                )

                app.run_merge_branch_job(
                    "missing-job",
                    "feature-cart",
                    {"activate": False},
                )
            finally:
                app.BRANCHES_DIR = original_branches_dir
                app.BRANCH_ACTIVE_FILE = original_active_file
                app.BRANCH_LOCK_FILE = original_lock_file

            merged_metadata = app.json.loads(
                (tmpdir / "branches" / "main" / "metadata.json").read_text()
            )
            self.assertEqual((tmpdir / "branches" / "main" / "public.dump").read_bytes(), b"feature")
            self.assertEqual(merged_metadata["name"], "main")
            self.assertEqual(merged_metadata["merged_from"], "feature-cart")
            self.assertEqual(merged_metadata["notes"], "Merged feature-cart into main")
            self.assertNotIn("source_branch", merged_metadata)

    def test_app_only_restore_precleans_and_restores_without_clean(self):
        original_branches_dir = app.BRANCHES_DIR
        original_branch_db_url = app.BRANCH_DB_URL
        original_sql = app.run_logged_sql
        original_command = app.run_logged_command
        calls = []

        def fake_sql(job_id, endpoint, sql):
            calls.append(("sql", sql))

        def fake_command(job_id, command, input_path=None):
            calls.append(("command", command, input_path))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app.BRANCHES_DIR = tmpdir / "branches"
            app.BRANCH_DB_URL = "postgres://user:pass@example.test/postgres"
            app.run_logged_sql = fake_sql
            app.run_logged_command = fake_command
            try:
                branch_dir = app.branch_dir("main")
                branch_dir.mkdir(parents=True)
                (branch_dir / "public.dump").write_bytes(b"dump")
                metadata = app.branch_snapshot_metadata(
                    "main",
                    "app-only",
                    False,
                    ["public"],
                    "test branch",
                )
                app.restore_branch_state("missing-job", "main", metadata, manage_services=False)
            finally:
                app.BRANCHES_DIR = original_branches_dir
                app.BRANCH_DB_URL = original_branch_db_url
                app.run_logged_sql = original_sql
                app.run_logged_command = original_command

        self.assertEqual(calls[0][0], "sql")
        self.assertIn("drop %s if exists %s cascade", calls[0][1])
        self.assertIn("join (values ('public'))", calls[0][1])
        self.assertEqual(calls[1][0], "command")
        self.assertNotIn("--clean", calls[1][1])
        self.assertIn("--schema", calls[1][1])

    def test_migration_ledger_restore_sql_replaces_schema_migrations(self):
        sql = app.migration_ledger_restore_sql(
            [
                {
                    "version": "20260529204637",
                    "name": "create_cart_tables",
                    "statements": ["select 1;"],
                }
            ]
        )

        self.assertIn("truncate table \"supabase_migrations\".\"schema_migrations\"", sql)
        self.assertIn("jsonb_array_elements_text", sql)
        self.assertIn("20260529204637", sql)

    def test_flattened_migration_sql_is_split_for_display(self):
        migration = {
            "version": "20260529204637",
            "name": "create_cart_tables",
            "statements": [
                " /* migration notes */ "
                "-- Anyone can create a session "
                "CREATE POLICY \"Anyone can insert a cart session\" "
                "ON cart_sessions FOR INSERT TO anon, authenticated WITH CHECK (true); "
                "CREATE TABLE IF NOT EXISTS cart_items ( "
                "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                "session_id uuid NOT NULL REFERENCES cart_sessions(id) ON DELETE CASCADE, "
                "quantity integer NOT NULL DEFAULT 1 CHECK (quantity >= 1), "
                "UNIQUE (session_id, quantity) ); "
                "CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$ "
                "BEGIN NEW.updated_at = now(); RETURN NEW; END; $$; "
                "DO $$ BEGIN IF NOT EXISTS ( SELECT 1 FROM pg_trigger WHERE tgname = 'x' ) "
                "THEN CREATE TRIGGER x BEFORE UPDATE ON cart_items "
                "FOR EACH ROW EXECUTE FUNCTION set_updated_at(); END IF; END $$; "
            ],
        }

        statements = app.migration_display_statements(migration)
        sql = app.migration_sql(migration)

        self.assertEqual(len(statements), 5)
        self.assertEqual(app.migration_statement_count(migration), 5)
        self.assertNotIn("Anyone can create a session", sql)
        self.assertEqual(
            statements[0],
            'DROP POLICY IF EXISTS "Anyone can insert a cart session" ON cart_sessions;',
        )
        self.assertIn('CREATE POLICY "Anyone can insert a cart session"', statements[1])
        self.assertIn("\n  ON cart_sessions", statements[1])
        self.assertIn("CREATE TABLE IF NOT EXISTS cart_items (\n  id uuid", statements[2])
        self.assertIn("RETURN NEW;", statements[3])
        self.assertIn("CREATE TRIGGER x", statements[4])

    def test_schema_payload_marks_app_only_selectable_schemas(self):
        payload = app.annotate_database_schemas(
            {
                "database_name": "postgres",
                "schemas": [
                    {"name": "auth", "table_count": 2, "total_bytes": 10},
                    {"name": "public", "table_count": 3, "total_bytes": 20},
                ],
            }
        )

        by_name = {item["name"]: item for item in payload["schemas"]}
        self.assertFalse(by_name["auth"]["selectable_for_app_only"])
        self.assertTrue(by_name["auth"]["excluded_from_app_only"])
        self.assertTrue(by_name["public"]["selectable_for_app_only"])
        self.assertFalse(by_name["public"]["excluded_from_app_only"])
        self.assertEqual(payload["default_app_schemas"], ["public"])

    def test_parse_schema_roles(self):
        self.assertEqual(app.parse_schema_roles({}), ["source", "target"])
        self.assertEqual(app.parse_schema_roles({"role": ["source"]}), ["source"])
        self.assertEqual(app.parse_schema_roles({"role": ["target"]}), ["target"])

        with self.assertRaises(ValueError):
            app.parse_schema_roles({"role": ["invalid"]})

    def test_first_feature_branch_creates_main_baseline(self):
        original_branches_dir = app.BRANCHES_DIR
        original_active_file = app.BRANCH_ACTIVE_FILE
        original_lock_file = app.BRANCH_LOCK_FILE
        original_snapshot = app.snapshot_branch_state
        snapshots = []

        def fake_snapshot(
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
            snapshots.append((name, source_branch))
            metadata = app.branch_snapshot_metadata(
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
            app.branch_dir(name).mkdir(parents=True, exist_ok=True)
            app.write_branch_metadata(name, metadata)
            return metadata

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            app.BRANCHES_DIR = tmpdir / "branches"
            app.BRANCH_ACTIVE_FILE = tmpdir / ".branches-active"
            app.BRANCH_LOCK_FILE = tmpdir / ".branches-lock"
            app.snapshot_branch_state = fake_snapshot
            try:
                app.run_create_branch_job(
                    "missing-job",
                    {
                        "name": "feature-cart",
                        "mode": "app-only",
                        "include_table_data": True,
                        "include_storage_files": False,
                        "schemas": ["public"],
                        "notes": "Cart Feature",
                        "overwrite": False,
                        "activate": True,
                        "no_owner": True,
                        "no_privileges": False,
                    },
                )
            finally:
                app.BRANCHES_DIR = original_branches_dir
                app.BRANCH_ACTIVE_FILE = original_active_file
                app.BRANCH_LOCK_FILE = original_lock_file
                app.snapshot_branch_state = original_snapshot

            self.assertEqual(snapshots, [("main", None), ("feature-cart", "main")])
            self.assertEqual((tmpdir / ".branches-active").read_text().strip(), "feature-cart")
            self.assertTrue((tmpdir / "branches" / "main" / "metadata.json").exists())
            self.assertEqual(
                app.json.loads((tmpdir / "branches" / "feature-cart" / "metadata.json").read_text())[
                    "source_branch"
                ],
                "main",
            )


class BranchingDocumentationTests(unittest.TestCase):
    def test_openapi_exposes_branded_public_definition(self):
        definition = app.read_openapi_definition()
        route = definition["paths"]["/v1/jrp-supabase-slim.json"]["get"]

        self.assertEqual(route["security"], [])
        self.assertIn("application/json", route["responses"]["200"]["content"])
        self.assertNotIn("/v1/openapi.json", definition["paths"])
        self.assertIn("/v1/imports/platform-to-local", definition["paths"])
        self.assertIn("/v1/imports.md", definition["paths"])
        self.assertIn("progress", definition["components"]["schemas"]["JobSummary"]["properties"])
        self.assertIn("JobProgress", definition["components"]["schemas"])

    def test_openapi_exposes_public_branching_guide(self):
        definition = app.read_openapi_definition()
        route = definition["paths"]["/v1/branching.md"]["get"]

        self.assertEqual(route["security"], [])
        self.assertIn("text/markdown", route["responses"]["200"]["content"])

    def test_openapi_exposes_public_import_guide(self):
        definition = app.read_openapi_definition()
        route = definition["paths"]["/v1/imports.md"]["get"]

        self.assertEqual(route["security"], [])
        self.assertIn("text/markdown", route["responses"]["200"]["content"])

    def test_branching_doc_reads_configured_markdown_file(self):
        original_doc_file = app.BRANCHING_DOC_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            doc_file = Path(tmpdir) / "branching.md"
            doc_file.write_text("# Branching\n")
            app.BRANCHING_DOC_FILE = doc_file
            try:
                self.assertEqual(app.read_branching_doc(), "# Branching\n")
            finally:
                app.BRANCHING_DOC_FILE = original_doc_file

    def test_import_doc_reads_configured_markdown_file(self):
        original_doc_file = app.IMPORT_DOC_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            doc_file = Path(tmpdir) / "imports.md"
            doc_file.write_text("# Imports\n")
            app.IMPORT_DOC_FILE = doc_file
            try:
                self.assertEqual(app.read_import_doc(), "# Imports\n")
            finally:
                app.IMPORT_DOC_FILE = original_doc_file


class ImportPlanTests(unittest.TestCase):
    def test_import_plan_requires_source_identity(self):
        with self.assertRaises(ValueError):
            app.build_import_plan({"source": {}})

    def test_import_plan_keeps_tool_state_outside_supabase_databases(self):
        plan = app.build_import_plan(
            {
                "database_mode": "schema-only",
                "source": {
                    "type": "platform",
                    "project_ref": "abcdefghijklmnopqrst",
                },
                "target": {
                    "type": "local",
                },
            }
        )

        self.assertEqual(plan["kind"], "import_plan")
        self.assertFalse(plan["control_plane"]["uses_source_database_for_tool_state"])
        self.assertFalse(plan["control_plane"]["uses_target_database_for_tool_state"])
        self.assertFalse(plan["feasibility"]["requires_tool_database"])
        self.assertTrue(plan["feasibility"]["platform_to_local_endpoint_available"])
        self.assertEqual(plan["target"]["side"]["container"], "supabase-db")
        self.assertFalse(plan["source"]["database"]["available"])

    def test_import_plan_normalizes_legacy_local_target_container(self):
        plan = app.build_import_plan(
            {
                "database_mode": "schema-only",
                "source": {
                    "type": "platform",
                    "project_ref": "abcdefghijklmnopqrst",
                },
                "target": {
                    "type": "local",
                    "container": "supabase_db_local",
                },
            }
        )

        self.assertEqual(plan["target"]["side"]["container"], "supabase-db")

    def test_import_plan_summarizes_database_when_connection_available(self):
        original_psql_json = app.psql_json

        def fake_psql_json(endpoint, sql):
            if "pg_database_size" in sql:
                return {
                    "database_name": "postgres",
                    "current_user": "postgres",
                    "server_version": "16.1",
                    "server_version_num": 160001,
                    "database_size_bytes": 123456,
                    "extensions": [
                        {
                            "name": "pgcrypto",
                            "schema": "extensions",
                            "version": "1.3",
                        }
                    ],
                    "has_storage_buckets": True,
                    "has_supabase_migrations": True,
                }
            if "has_storage_buckets" in sql:
                return {
                    "database_name": "postgres",
                    "table_count": 1,
                    "tables": [
                        {
                            "schema": "public",
                            "name": "orders",
                            "row_count": 42,
                            "row_count_exact": False,
                            "total_bytes": 8192,
                        }
                    ],
                    "has_storage_buckets": True,
                }
            if "from storage.buckets" in sql:
                return [{"id": "images", "name": "images", "public": True}]
            raise AssertionError(f"Unexpected SQL: {sql}")

        app.psql_json = fake_psql_json
        try:
            plan = app.build_import_plan(
                {
                    "database_mode": "schema-and-data",
                    "source": {
                        "type": "platform",
                        "db_url": "postgres://postgres:secret@db.example.supabase.co:5432/postgres",
                    },
                    "target": {
                        "type": "local",
                        "container": "supabase-db",
                    },
                }
            )
        finally:
            app.psql_json = original_psql_json

        self.assertTrue(plan["source"]["database"]["available"])
        self.assertTrue(plan["target"]["database"]["available"])
        self.assertEqual(plan["source"]["database"]["table_count"], 1)
        self.assertEqual(plan["source"]["database"]["storage_bucket_count"], 1)
        self.assertTrue(plan["feasibility"]["can_run_platform_to_local_now"])

    def test_supabase_account_helpers_use_management_api(self):
        original_management_api_json = app.management_api_json
        calls = []

        class Handler:
            path = "/v1/supabase/projects?organization_id=org_123"

            class Headers:
                def get(self, key):
                    if key == "X-Supabase-Access-Token":
                        return "supabase-token"
                    return None

            headers = Headers()

        def fake_management_api_json(endpoint, path, method="GET", payload=None):
            calls.append((endpoint["access_token"], path))
            return {"projects": [{"ref": "project-ref"}]}

        app.management_api_json = fake_management_api_json
        try:
            response = app.list_supabase_projects(Handler())
        finally:
            app.management_api_json = original_management_api_json

        self.assertEqual(response["projects"], [{"ref": "project-ref"}])
        self.assertEqual(calls, [("supabase-token", "/v1/projects?organization_id=org_123")])

    def test_platform_to_local_requires_confirmation(self):
        with self.assertRaises(ValueError):
            app.parse_platform_to_local_options(
                {
                    "source": {
                        "type": "platform",
                        "db_url": "postgres://postgres:secret@example.test/postgres",
                    },
                    "target": {"type": "local"},
                }
            )

        options = app.parse_platform_to_local_options(
            {
                "dry_run": True,
                "source": {
                    "type": "platform",
                    "db_url": "postgres://postgres:secret@example.test/postgres",
                },
                "target": {"type": "local"},
            }
        )

        self.assertTrue(options["dry_run"])

    def test_platform_to_local_rejects_schema_data_without_auth_data(self):
        with self.assertRaises(ValueError):
            app.parse_platform_to_local_options(
                {
                    "confirm": "CONFIRM",
                    "database_mode": "schema-and-data",
                    "include_auth_data": False,
                    "source": {
                        "type": "platform",
                        "db_url": "postgres://postgres:secret@example.test/postgres",
                    },
                    "target": {"type": "local"},
                }
            )

    def test_platform_to_local_defaults_target_to_local_supabase_db(self):
        config, source, target = app.platform_to_local_config(
            {
                "source": {
                    "type": "platform",
                    "project_ref": "project-ref",
                    "db_url": "postgres://postgres:secret@example.test/postgres",
                },
                "target": {"type": "local"},
            }
        )

        self.assertEqual(source["project_ref"], "project-ref")
        self.assertEqual(target["container"], "supabase-db")
        self.assertEqual(config["target_container"], "supabase-db")

    def test_platform_to_local_normalizes_legacy_local_target_container(self):
        config, source, target = app.platform_to_local_config(
            {
                "source": {
                    "type": "platform",
                    "project_ref": "project-ref",
                    "db_url": "postgres://postgres:secret@example.test/postgres",
                },
                "target": {
                    "type": "local",
                    "container": "supabase_db_local",
                },
            }
        )

        self.assertEqual(target["container"], "supabase-db")
        self.assertEqual(config["target_container"], "supabase-db")

    def test_start_platform_to_local_import_queues_job(self):
        original_start_job = app.start_job
        calls = []

        def fake_start_job(kind, env_name, command, runner=None):
            calls.append((kind, env_name, command, runner))
            return {"id": "job-id", "kind": kind, "status": "queued"}

        app.start_job = fake_start_job
        try:
            job = app.start_platform_to_local_import(
                {
                    "confirm": "CONFIRM",
                    "database_mode": "schema-only",
                    "source": {
                        "type": "platform",
                        "project_ref": "project-ref",
                        "db_url": "postgres://postgres:secret@example.test/postgres",
                    },
                    "target": {"type": "local"},
                }
            )
        finally:
            app.start_job = original_start_job

        self.assertEqual(job["id"], "job-id")
        self.assertEqual(calls[0][0], "import_platform_to_local")
        self.assertIn("--schema-only", calls[0][2])

    def test_job_progress_merges_details(self):
        job_id = "progress-test"
        with app.jobs_lock:
            app.jobs[job_id] = {
                "id": job_id,
                "progress": {
                    "phase": "queued",
                    "percent": 0,
                    "message": "Queued",
                    "updated_at_ms": 1,
                    "details": {"existing": True},
                },
            }

        try:
            app.update_job_progress(
                job_id,
                "database_import",
                35,
                "Importing database",
                {"table_count": 12},
            )
            with app.jobs_lock:
                progress = dict(app.jobs[job_id]["progress"])
        finally:
            with app.jobs_lock:
                app.jobs.pop(job_id, None)

        self.assertEqual(progress["phase"], "database_import")
        self.assertEqual(progress["percent"], 35)
        self.assertEqual(progress["message"], "Importing database")
        self.assertEqual(
            progress["details"],
            {"existing": True, "table_count": 12},
        )
        with app.jobs_lock:
            app.jobs[job_id] = {
                "id": job_id,
                "progress_events": [],
            }
        try:
            app.update_job_progress(job_id, "planning", 10, "Planning")
            app.update_job_progress(job_id, "planned", 20, "Planned")
            with app.jobs_lock:
                phases = [
                    event["phase"]
                    for event in app.jobs[job_id]["progress_events"]
                ]
        finally:
            with app.jobs_lock:
                app.jobs.pop(job_id, None)

        self.assertEqual(phases, ["planning", "planned"])


if __name__ == "__main__":
    unittest.main()
