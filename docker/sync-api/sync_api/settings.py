import base64
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


HOST = os.environ.get("SYNC_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("SYNC_API_PORT", "8080"))
DATA_DIR = Path(os.environ.get("SYNC_API_DATA_DIR", "/data"))
ENVIRONMENTS_FILE = DATA_DIR / "environments.json"
LOG_FILE = Path(os.environ.get("SYNC_API_LOG_FILE", str(DATA_DIR / "sync-api.log")))
LOG_BODY_MAX_CHARS = int(os.environ.get("SYNC_API_LOG_BODY_MAX_CHARS", "4000"))
OPENAPI_FILE = Path(
    os.environ.get("SYNC_API_OPENAPI_FILE", "/app/jrp-supabase-slim.json")
)
BRANCHING_DOC_FILE = Path(
    os.environ.get("SYNC_API_BRANCHING_DOC_FILE", "/app/branching.md")
)
IMPORT_DOC_FILE = Path(
    os.environ.get("SYNC_API_IMPORT_DOC_FILE", "/app/imports.md")
)
PROMOTE_SCRIPT = os.environ.get(
    "PROMOTE_MIGRATIONS_SCRIPT", "/opt/sync-api/promote-migrations.sh"
)
API_TOKEN = os.environ.get("SYNC_API_TOKEN", "")
STATS_TIMEOUT_SECONDS = int(os.environ.get("SYNC_API_STATS_TIMEOUT_SECONDS", "30"))
STATS_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("SYNC_API_STATS_STATEMENT_TIMEOUT_MS", str(STATS_TIMEOUT_SECONDS * 1000))
)
EDGE_FUNCTIONS_DIR = Path(
    os.environ.get("SYNC_API_EDGE_FUNCTIONS_DIR", "/edge-functions")
)
FUNCTIONS_API_BASE = os.environ.get(
    "SYNC_API_FUNCTIONS_API_BASE", "https://api.supabase.com"
).rstrip("/")
EDGE_SOURCE_MAX_FILE_BYTES = int(
    os.environ.get("SYNC_API_EDGE_SOURCE_MAX_FILE_BYTES", str(1024 * 1024))
)
RESET_TMP_DIR = Path(os.environ.get("SYNC_API_RESET_TMP_DIR", str(DATA_DIR)))
BRANCHES_DIR = Path(os.environ.get("SYNC_API_BRANCHES_DIR", str(DATA_DIR / "branches")))
BRANCH_ACTIVE_FILE = Path(
    os.environ.get("SYNC_API_BRANCH_ACTIVE_FILE", str(DATA_DIR / ".branches-active"))
)
BRANCH_LOCK_FILE = Path(
    os.environ.get("SYNC_API_BRANCH_LOCK_FILE", str(DATA_DIR / ".branches-lock"))
)
BRANCH_STORAGE_DIR = Path(
    os.environ.get("SYNC_API_BRANCH_STORAGE_DIR", "/supabase-storage")
)
BRANCH_DB_URL = os.environ.get("SYNC_API_BRANCH_DB_URL", "")
BRANCH_DB_CONTAINER = os.environ.get("SYNC_API_BRANCH_DB_CONTAINER", "supabase-db")
BRANCH_DB_USER = os.environ.get("SYNC_API_BRANCH_DB_USER", "supabase_admin")
BRANCH_DB_NAME = os.environ.get("SYNC_API_BRANCH_DB_NAME", "postgres")
BRANCH_FULL_SERVICE_CONTAINERS = os.environ.get(
    "SYNC_API_BRANCH_FULL_SERVICE_CONTAINERS",
    (
        "supabase-auth supabase-rest realtime-dev.supabase-realtime "
        "supabase-storage supabase-studio supabase-kong"
    ),
)
BRANCH_APP_RESTART_CONTAINERS = os.environ.get(
    "SYNC_API_BRANCH_APP_RESTART_CONTAINERS",
    "supabase-rest realtime-dev.supabase-realtime supabase-storage supabase-kong",
)
BRANCH_STORAGE_SERVICE_CONTAINERS = os.environ.get(
    "SYNC_API_BRANCH_STORAGE_SERVICE_CONTAINERS",
    "supabase-storage supabase-imgproxy",
)
BRANCH_DEFAULT_APP_SCHEMAS = os.environ.get(
    "SYNC_API_BRANCH_DEFAULT_APP_SCHEMAS", "public"
)
BRANCH_DEFAULT_BASELINE_NAME = os.environ.get(
    "SYNC_API_BRANCH_DEFAULT_BASELINE_NAME", "main"
)
PROTECTED_TARGET_ENVIRONMENTS = {
    item.strip().lower()
    for item in re.split(
        r"[\s,]+",
        os.environ.get("SYNC_API_PROTECTED_TARGET_ENVIRONMENTS", "production prod"),
    )
    if item.strip()
}
BRANCH_MIGRATION_LEDGER_SCHEMA = os.environ.get(
    "SYNC_API_BRANCH_MIGRATION_LEDGER_SCHEMA", "supabase_migrations"
)
BRANCH_MIGRATION_LEDGER_TABLE = os.environ.get(
    "SYNC_API_BRANCH_MIGRATION_LEDGER_TABLE", "schema_migrations"
)
MIGRATION_PROMOTION_LEDGER_SCHEMA = os.environ.get(
    "MIGRATION_LEDGER_SCHEMA", "_migrations"
)
MIGRATION_PROMOTION_LEDGER_TABLE = os.environ.get(
    "MIGRATION_LEDGER_TABLE", "promoted_schema_migrations"
)
LOCAL_PROJECT_NAME = (
    os.environ.get("SYNC_API_LOCAL_PROJECT_NAME")
    or os.environ.get("DEFAULT_PROJECT_NAME")
    or os.environ.get("STUDIO_DEFAULT_PROJECT")
)
LOCAL_ORGANIZATION_NAME = (
    os.environ.get("SYNC_API_LOCAL_ORGANIZATION_NAME")
    or os.environ.get("DEFAULT_ORGANIZATION_NAME")
    or os.environ.get("STUDIO_DEFAULT_ORGANIZATION")
)

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
MIGRATION_STATEMENT_START = r"""
(?:
  CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|POLICY|(?:UNIQUE\s+)?INDEX|SCHEMA|EXTENSION|TRIGGER|FUNCTION|PROCEDURE|TYPE|VIEW|MATERIALIZED\s+VIEW|SEQUENCE)
  | ALTER\s+TABLE
  | DROP\s+(?:POLICY|TABLE|INDEX|SCHEMA|TRIGGER|FUNCTION|PROCEDURE|TYPE|VIEW|MATERIALIZED\s+VIEW|SEQUENCE)
  | GRANT\b
  | REVOKE\b
  | COMMENT\s+ON
  | TRUNCATE\b
  | INSERT\s+INTO
  | UPDATE\s+\S+\s+SET
  | DELETE\s+FROM
  | SELECT\s+
  | DO\s+\$\$
)
"""
MIGRATION_FLATTENED_LINE_COMMENT_RE = re.compile(
    rf"(?:^|\s)--\s.*?\s(?={MIGRATION_STATEMENT_START})",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)
MIGRATION_CREATE_POLICY_RE = re.compile(
    r'CREATE\s+POLICY\s+("[^"]+"|\S+)\s+ON\s+([^\s]+)',
    re.IGNORECASE,
)
SKIP_EDGE_SOURCE_DIRS = {
    ".git",
    ".next",
    ".turbo",
    "node_modules",
}
SECRET_CONFIG_KEYS = {
    "source_supabase_access_token",
    "target_supabase_access_token",
}
SECRET_LOG_KEY_PARTS = (
    "authorization",
    "access_token",
    "api_key",
    "apikey",
    "bearer",
    "client_secret",
    "db_pass",
    "db_url",
    "jwt",
    "key",
    "password",
    "secret",
    "service_role",
    "token",
)
SKIP_SOURCE_PLATFORM_SCHEMAS_FOR_HOSTED_RESTORE = {
    "_realtime",
    "extensions",
    "graphql",
    "graphql_public",
    "pgbouncer",
    "supabase_functions",
    "vault",
}

store_lock = threading.Lock()
jobs_lock = threading.Lock()
log_lock = threading.Lock()
branch_operation_lock = threading.Lock()
jobs = {}
