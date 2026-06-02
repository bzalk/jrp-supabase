from .settings import *

def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

def parse_bool_query(query, key, default=False):
    values = query.get(key)
    if not values:
        return default
    value = values[-1].lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key} must be a boolean")


def parse_bool_body(body, key, default=False):
    if key not in body:
        return default
    value = body[key]
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be a boolean")


def split_config_list(value):
    if not value:
        return []
    return [item for item in re.split(r"[\s,]+", value.strip()) if item]


def parse_string_query(query, key, default=None):
    values = query.get(key)
    if not values:
        if default is None:
            raise ValueError(f"{key} is required")
        return default
    value = values[-1].strip()
    if not value:
        raise ValueError(f"{key} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{key} must not contain null bytes")
    return value


def sql_literal(value):
    if "\x00" in value:
        raise ValueError("SQL literal must not contain null bytes")
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(value):
    if "\x00" in value:
        raise ValueError("SQL identifier must not contain null bytes")
    return '"' + value.replace('"', '""') + '"'
