from .settings import *
from .http_utils import parse_bool_body


APP_ONLY_EXCLUDED_SCHEMAS = [
    "auth",
    "storage",
    "realtime",
    "_realtime",
    "supabase_functions",
    "extensions",
    "graphql",
    "graphql_public",
    "net",
    "pgbouncer",
    "supabase_migrations",
    "vault",
]

def split_config_list(value):
    if not value:
        return []
    return [item for item in re.split(r"[\s,]+", value.strip()) if item]


def validate_branch_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("branch name must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
    return name


def normalize_branch_mode(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("mode must be full or app-only")
    mode = value.strip().lower().replace("_", "-")
    if mode not in ("full", "app-only"):
        raise ValueError("mode must be full or app-only")
    return mode


def parse_branch_mode(body, default=None):
    requested = []
    explicit_mode = normalize_branch_mode(body.get("mode")) if "mode" in body else None
    if explicit_mode:
        requested.append(explicit_mode)
    if body.get("full") is True:
        requested.append("full")
    if body.get("app_only") is True or body.get("app-only") is True:
        requested.append("app-only")
    if len(set(requested)) > 1:
        raise ValueError("branch mode is ambiguous")
    if requested:
        return requested[0]
    if default:
        return default
    raise ValueError("mode is required; use full or app-only")


def normalize_branch_data_mode(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("data_mode must be schema-and-data or schema-only")
    mode = value.strip().lower().replace("_", "-")
    aliases = {
        "with-data": "schema-and-data",
        "data": "schema-and-data",
        "schema-data": "schema-and-data",
        "schema-and-data": "schema-and-data",
        "schema-only": "schema-only",
        "no-data": "schema-only",
        "without-data": "schema-only",
    }
    normalized = aliases.get(mode)
    if not normalized:
        raise ValueError("data_mode must be schema-and-data or schema-only")
    return normalized


def parse_branch_include_table_data(body, default=True):
    requested = []
    explicit_mode = normalize_branch_data_mode(body.get("data_mode")) if "data_mode" in body else None
    if explicit_mode:
        requested.append(explicit_mode != "schema-only")
    if "include_table_data" in body:
        requested.append(parse_bool_body(body, "include_table_data", default))
    if "schema_only" in body:
        requested.append(not parse_bool_body(body, "schema_only", False))
    if len(set(requested)) > 1:
        raise ValueError("branch data mode is ambiguous")
    if requested:
        return requested[0]
    return default


def branch_includes_table_data(metadata):
    if "includes_table_data" in metadata:
        return bool(metadata["includes_table_data"])
    if metadata.get("data_mode") == "schema-only":
        return False
    return True


def validate_schema_names(value, default=None, allow_star=False):
    if value is None:
        schemas = list(default or [])
    elif isinstance(value, str):
        schemas = split_config_list(value)
    elif isinstance(value, list):
        schemas = value
    else:
        raise ValueError("schemas must be a string or array of strings")

    normalized = []
    for schema_name in schemas:
        if not isinstance(schema_name, str):
            raise ValueError("schemas must contain only strings")
        schema_name = schema_name.strip()
        if not schema_name:
            raise ValueError("schemas must not contain empty values")
        if "\x00" in schema_name:
            raise ValueError("schemas must not contain null bytes")
        if schema_name == "*" and not allow_star:
            raise ValueError("schemas may only contain * for full branches")
        normalized.append(schema_name)
    if not normalized:
        raise ValueError("schemas must contain at least one schema")
    return normalized


def branch_database_endpoint():
    if BRANCH_DB_URL:
        return {"kind": "url", "db_url": BRANCH_DB_URL}
    return {
        "kind": "container",
        "container": BRANCH_DB_CONTAINER,
        "user": BRANCH_DB_USER,
        "database": BRANCH_DB_NAME,
    }


def ensure_branches_dir():
    BRANCHES_DIR.mkdir(parents=True, exist_ok=True)


def branch_dir(name):
    return BRANCHES_DIR / validate_branch_name(name)


def branch_metadata_path(name):
    return branch_dir(name) / "metadata.json"


def branch_exists(name):
    return branch_metadata_path(name).exists()


def branch_dump_filename(mode):
    return "db.dump" if mode == "full" else "public.dump"


def branch_dump_path(name, mode):
    return branch_dir(name) / branch_dump_filename(mode)


def branch_storage_archive_path(name):
    return branch_dir(name) / "storage.tar.gz"


def branch_migration_ledger_path(name):
    return branch_dir(name) / "schema_migrations.json"


def read_branch_metadata(name):
    metadata_file = branch_metadata_path(name)
    if not metadata_file.exists():
        raise ValueError("Branch not found")
    metadata = json.loads(metadata_file.read_text())
    metadata["name"] = name
    return metadata


def write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(path)


def write_branch_metadata(name, metadata):
    write_json_atomic(branch_metadata_path(name), metadata)


def read_active_branch():
    if not BRANCH_ACTIVE_FILE.exists():
        return None
    value = BRANCH_ACTIVE_FILE.read_text().strip()
    return value or None


def write_active_branch(name):
    validate_branch_name(name)
    BRANCH_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BRANCH_ACTIVE_FILE.write_text(name + "\n")


def file_size_or_none(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


def public_branch(metadata, active_branch=None):
    name = metadata["name"]
    mode = metadata.get("mode", "full")
    storage_path = branch_storage_archive_path(name)
    dump_path = branch_dump_path(name, mode)
    includes_table_data = branch_includes_table_data(metadata)
    return {
        **metadata,
        "active": active_branch == name,
        "includes_table_data": includes_table_data,
        "data_mode": "schema-and-data" if includes_table_data else "schema-only",
        "dump_file": branch_dump_filename(mode),
        "dump_size_bytes": file_size_or_none(dump_path),
        "storage_size_bytes": file_size_or_none(storage_path),
    }


def list_branch_metadata():
    ensure_branches_dir()
    branches = []
    for item in sorted(BRANCHES_DIR.iterdir()):
        if not item.is_dir():
            continue
        metadata_file = item / "metadata.json"
        if not metadata_file.exists():
            continue
        metadata = json.loads(metadata_file.read_text())
        metadata["name"] = item.name
        branches.append(metadata)
    return branches


def clear_branch_registry():
    with branch_operation_lock:
        ensure_branches_dir()
        removed = 0
        for item in BRANCHES_DIR.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
                removed += 1
            else:
                item.unlink()
                removed += 1
        BRANCH_ACTIVE_FILE.unlink(missing_ok=True)
        return removed
