from .settings import *
from .audit import *
from .management import *
from .http_utils import parse_bool_body

def project_domain(project_ref):
    if not project_ref:
        return None
    return f"https://{project_ref}.supabase.co"


def infer_project_ref_from_db_url(value):
    if not value:
        return None

    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    username = parsed.username or ""

    if username.startswith("postgres."):
        project_ref = username.removeprefix("postgres.")
        if project_ref:
            return project_ref

    if hostname.endswith(".supabase.co"):
        labels = hostname.split(".")
        if len(labels) >= 4 and labels[0] == "db":
            return labels[1]
        if len(labels) == 3:
            return labels[0]

    return None


def project_ref_from_config(config, role):
    return (
        config.get(f"{role}_project_ref")
        or infer_project_ref_from_db_url(config.get(f"{role}_db_url"))
    )


def database_identity_from_config(config, role):
    db_url = config.get(f"{role}_db_url")
    if db_url:
        parsed = urlparse(db_url)
        project_ref = infer_project_ref_from_db_url(db_url)
        database = parsed.path.lstrip("/") or None
        return {
            "kind": "url",
            "host": parsed.hostname,
            "port": parsed.port,
            "database": database,
            "container": None,
            "project_ref": project_ref,
            "project_id": project_ref,
            "domain": project_domain(project_ref),
        }

    container = config.get(f"{role}_container")
    if role == "source" and not container:
        container = "supabase-db"

    return {
        "kind": "container",
        "host": None,
        "port": None,
        "database": config.get(f"{role}_db_name", "postgres"),
        "container": container,
        "project_ref": None,
        "project_id": None,
        "domain": None,
    }


def edge_functions_identity_from_config(config, role):
    configured_dir = config.get(f"{role}_edge_functions_dir")
    if configured_dir:
        return {
            "kind": "local",
            "root": configured_dir,
            "api_base": None,
            "project_ref": None,
            "project_id": None,
            "domain": None,
        }

    project_ref = project_ref_from_config(config, role)
    if project_ref:
        return {
            "kind": "management_api",
            "root": None,
            "api_base": config.get(f"{role}_functions_api_base", FUNCTIONS_API_BASE).rstrip("/"),
            "project_ref": project_ref,
            "project_id": project_ref,
            "domain": project_domain(project_ref),
        }

    if role == "source":
        return {
            "kind": "local",
            "root": str(EDGE_FUNCTIONS_DIR),
            "api_base": None,
            "project_ref": None,
            "project_id": None,
            "domain": None,
        }

    return None


def management_api_endpoint_from_config(config, role, project_ref):
    return {
        "api_base": config.get(f"{role}_functions_api_base", FUNCTIONS_API_BASE).rstrip("/"),
        "project_ref": project_ref,
        "access_token": config.get(f"{role}_supabase_access_token")
        or os.environ.get("SUPABASE_ACCESS_TOKEN"),
    }


def project_metadata_from_config(config, role, project_ref):
    if not project_ref:
        return None, "Project ref is not configured or inferable"

    endpoint = management_api_endpoint_from_config(config, role, project_ref)
    if not endpoint.get("access_token"):
        return None, "Supabase access token is not configured"

    try:
        return management_api_json(
            endpoint, f"/v1/projects/{quote(project_ref, safe='')}"
        ), None
    except Exception as exc:
        return None, str(exc)


def organization_metadata_from_config(config, role, organization_id):
    if not organization_id:
        return None

    endpoint = management_api_endpoint_from_config(config, role, None)
    if not endpoint.get("access_token"):
        return None

    try:
        payload = management_api_json(endpoint, "/v1/organizations")
    except Exception:
        return None

    if isinstance(payload, dict):
        organizations = payload.get("organizations", [])
    elif isinstance(payload, list):
        organizations = payload
    else:
        organizations = []

    for organization in organizations:
        if not isinstance(organization, dict):
            continue
        if organization_id in (
            organization.get("id"),
            organization.get("slug"),
            organization.get("organization_id"),
            organization.get("organization_slug"),
        ):
            return organization
    return None


def project_metadata_value(metadata, *keys):
    if not isinstance(metadata, dict):
        return None
    for key in keys:
        value = metadata.get(key)
        if value not in (None, ""):
            return value
    return None


def project_organization_name(metadata):
    organization = project_metadata_value(metadata, "organization")
    if isinstance(organization, dict):
        return project_metadata_value(organization, "name", "slug", "id")
    if isinstance(organization, str):
        return organization
    return project_metadata_value(metadata, "organization_name", "organization_slug")


def local_identity_value(config, role, key, default_value):
    return config.get(f"{role}_{key}") or config.get(key) or default_value


def is_local_project_identity(database, edge_functions):
    if database.get("kind") == "container":
        return True
    return bool(edge_functions and edge_functions.get("kind") == "local")


def environment_project_identity(env_name, config, role):
    database = database_identity_from_config(config, role)
    edge_functions = edge_functions_identity_from_config(config, role)
    project_ref = (
        config.get(f"{role}_project_ref")
        or database.get("project_ref")
        or (edge_functions or {}).get("project_ref")
    )
    local_identity = is_local_project_identity(database, edge_functions)
    if project_ref:
        metadata, metadata_error = project_metadata_from_config(config, role, project_ref)
    else:
        metadata = None
        metadata_error = None if local_identity else "Project ref is not configured or inferable"
    metadata_project_ref = project_metadata_value(metadata, "ref", "project_ref")
    project_ref = metadata_project_ref or project_ref
    project_id = project_metadata_value(metadata, "id", "project_id") or project_ref
    domain = project_domain(project_ref) or database.get("host")
    project_name = project_metadata_value(metadata, "name")
    organization_id = project_metadata_value(metadata, "organization_id", "organization_slug")
    organization_metadata = organization_metadata_from_config(config, role, organization_id)
    organization_name = (
        project_metadata_value(organization_metadata, "name")
        or project_organization_name(metadata)
    )
    if local_identity:
        project_name = project_name or local_identity_value(
            config, role, "project_name", LOCAL_PROJECT_NAME
        )
        organization_name = organization_name or local_identity_value(
            config, role, "organization_name", LOCAL_ORGANIZATION_NAME
        )

    return {
        "role": role,
        "environment": config.get(f"{role}_env", "dev" if role == "source" else env_name),
        "project_ref": project_ref,
        "project_id": project_id,
        "name": project_name,
        "region": project_metadata_value(metadata, "region", "region_code"),
        "organization_id": organization_id,
        "organization_name": organization_name,
        "domain": domain,
        "project_metadata_available": metadata is not None,
        "project_metadata_error": metadata_error,
        "database": database,
        "edge_functions": edge_functions,
    }


def environment_identity(env_name, config):
    return {
        "environment": env_name,
        "generated_at_ms": now_ms(),
        "projects": [
            environment_project_identity(env_name, config, "source"),
            environment_project_identity(env_name, config, "target"),
        ],
    }


def validate_environment_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("name must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
    return name


def environment_label_is_protected(value):
    if not isinstance(value, str):
        return False
    return value.strip().lower() in PROTECTED_TARGET_ENVIRONMENTS


def default_environment_protected(name, config):
    return environment_label_is_protected(name) or environment_label_is_protected(
        config.get("target_env")
    )


def environment_is_protected(name, config):
    if "protected" in config:
        return config["protected"] is True
    return default_environment_protected(name, config)


def validate_environment_up_branch(env_name, config):
    if not environment_is_protected(env_name, config):
        return
    from .branches import read_active_branch, validate_branch_name

    main_branch = validate_branch_name(BRANCH_DEFAULT_BASELINE_NAME)
    active_branch = read_active_branch()
    if active_branch != main_branch:
        raise ValueError(
            (
                f"Protected environment {env_name} can only be promoted from "
                f"{main_branch}. Active branch is {active_branch or 'none'}; "
                f"merge and switch to {main_branch} before running up."
            )
        )


def apply_environment_side_setup(config, role, side, default_env, default_container, access_token):
    if side is None:
        side = {}
    if not isinstance(side, dict):
        raise ValueError(f"{role} setup must be an object")

    env_name = side.get("env", default_env)
    if not isinstance(env_name, str) or not env_name.strip():
        raise ValueError(f"{role}.env must be a non-empty string")
    config[f"{role}_env"] = env_name.strip()

    field_map = {
        "db_url": f"{role}_db_url",
        "container": f"{role}_container",
        "user": f"{role}_user",
        "reset_user": f"{role}_reset_user",
        "db_name": f"{role}_db_name",
        "edge_functions_dir": f"{role}_edge_functions_dir",
        "project_name": f"{role}_project_name",
        "organization_name": f"{role}_organization_name",
        "project_ref": f"{role}_project_ref",
        "functions_api_base": f"{role}_functions_api_base",
    }
    for input_key, config_key in field_map.items():
        value = side.get(input_key)
        if value is not None:
            config[config_key] = value

    if side.get("project_id") is not None and side.get("project_ref") is None:
        config[f"{role}_project_ref"] = side["project_id"]

    side_access_token = side.get("access_token", access_token)
    if side_access_token is not None:
        config[f"{role}_supabase_access_token"] = side_access_token

    if default_container and not config.get(f"{role}_db_url") and not config.get(f"{role}_container"):
        config[f"{role}_container"] = default_container


def environment_config_from_create_body(body):
    name = validate_environment_name(body.get("name"))
    config = {
        "name": name,
        "source_env": body.get("source_env", "dev"),
        "source_container": body.get("source_container", "supabase-db"),
        "target_env": body.get("target_env", name),
        "sync_storage_buckets": body.get("sync_storage_buckets", True),
    }
    config["protected"] = parse_bool_body(
        body,
        "protected",
        default_environment_protected(name, config),
    )

    for key in (
        "source_db_url",
        "source_user",
        "source_reset_user",
        "source_db_name",
        "target_db_url",
        "target_container",
        "target_user",
        "target_reset_user",
        "target_db_name",
        "source_edge_functions_dir",
        "target_edge_functions_dir",
        "source_project_name",
        "target_project_name",
        "source_organization_name",
        "target_organization_name",
        "source_project_ref",
        "target_project_ref",
        "source_supabase_access_token",
        "target_supabase_access_token",
        "source_functions_api_base",
        "target_functions_api_base",
        "batch_label",
    ):
        if body.get(key) is not None:
            config[key] = body[key]

    if not config.get("target_db_url") and not config.get("target_container"):
        raise ValueError("target_db_url or target_container is required")

    return name, config


def environment_config_from_setup_body(body):
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")

    name = validate_environment_name(body.get("name", "production"))
    access_token = body.get("access_token")
    config = {
        "name": name,
        "sync_storage_buckets": body.get("sync_storage_buckets", True),
    }

    apply_environment_side_setup(
        config,
        "source",
        body.get("dev"),
        "dev",
        "supabase-db",
        access_token,
    )
    apply_environment_side_setup(
        config,
        "target",
        body.get("production"),
        "production",
        None,
        access_token,
    )

    if body.get("batch_label") is not None:
        config["batch_label"] = body["batch_label"]

    config["protected"] = parse_bool_body(
        body,
        "protected",
        default_environment_protected(name, config),
    )

    if not config.get("target_db_url") and not config.get("target_container"):
        raise ValueError("production.db_url or production.container is required")

    return name, config
