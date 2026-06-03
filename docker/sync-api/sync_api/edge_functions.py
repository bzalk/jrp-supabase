from .settings import *
from .env_config import *
from .management import *
from .database import sha256_bytes, sha256_text

def edge_functions_endpoint_from_config(config, role):
    configured_dir = config.get(f"{role}_edge_functions_dir")
    if configured_dir:
        return {"kind": "local", "root": Path(configured_dir)}

    project_ref = project_ref_from_config(config, role)
    if project_ref:
        return {
            "kind": "management_api",
            "api_base": config.get(f"{role}_functions_api_base", FUNCTIONS_API_BASE).rstrip("/"),
            "project_ref": project_ref,
            "access_token": config.get(f"{role}_supabase_access_token")
            or os.environ.get("SUPABASE_ACCESS_TOKEN"),
        }

    if role == "source":
        return {"kind": "local", "root": EDGE_FUNCTIONS_DIR}

    return None


def edge_function_summary_hash(files):
    digest = hashlib.sha256()
    for file_item in sorted(files, key=lambda item: item["path"]):
        digest.update(edge_function_metadata_path(file_item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_item["sha256"].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def decode_source_file(path, raw, include_content, max_file_bytes=EDGE_SOURCE_MAX_FILE_BYTES):
    item = {
        "path": path,
        "size_bytes": len(raw),
        "sha256": sha256_bytes(raw),
    }
    if not include_content:
        return item

    content_raw = raw
    item["truncated"] = False
    if max_file_bytes is not None and len(content_raw) > max_file_bytes:
        content_raw = content_raw[:max_file_bytes]
        item["truncated"] = True

    try:
        item["encoding"] = "utf-8"
        item["content"] = content_raw.decode("utf-8")
    except UnicodeDecodeError:
        item["encoding"] = "base64"
        item["content_base64"] = base64.b64encode(content_raw).decode("ascii")
    return item


def iter_local_edge_function_files(
    function_dir,
    include_content,
    max_file_bytes=EDGE_SOURCE_MAX_FILE_BYTES,
):
    files = []
    for root, dirs, filenames in os.walk(function_dir):
        dirs[:] = [
            dirname
            for dirname in dirs
            if dirname not in SKIP_EDGE_SOURCE_DIRS and not dirname.startswith(".")
        ]
        for filename in sorted(filenames):
            file_path = Path(root) / filename
            if file_path.is_symlink() or not file_path.is_file():
                continue
            relative_path = file_path.relative_to(function_dir).as_posix()
            raw = file_path.read_bytes()
            item = decode_source_file(relative_path, raw, include_content, max_file_bytes)
            item["modified_at_ms"] = int(file_path.stat().st_mtime * 1000)
            files.append(item)
    files.sort(key=lambda item: item["path"])
    return files


def local_edge_entrypoint(files):
    paths = {file_item["path"] for file_item in files}
    for candidate in ("index.ts", "index.tsx", "index.js", "index.mjs"):
        if candidate in paths:
            return candidate
    for file_item in files:
        if file_item["path"].endswith((".ts", ".tsx", ".js", ".mjs")):
            return file_item["path"]
    return None


def local_edge_function_payload(
    root,
    slug,
    include_files=False,
    include_content=False,
    max_file_bytes=EDGE_SOURCE_MAX_FILE_BYTES,
):
    if not NAME_RE.match(slug):
        raise ValueError("edge function id must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
    function_dir = root / slug
    try:
        function_dir.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("edge function path escapes functions root") from exc
    if not function_dir.is_dir():
        raise FileNotFoundError(f"Edge function not found: {slug}")

    files = iter_local_edge_function_files(function_dir, include_content, max_file_bytes)
    updated_at_ms = max((file_item.get("modified_at_ms", 0) for file_item in files), default=None)
    payload = {
        "id": slug,
        "slug": slug,
        "name": slug,
        "status": "LOCAL",
        "entrypoint_path": local_edge_entrypoint(files),
        "file_count": len(files),
        "total_bytes": sum(file_item["size_bytes"] for file_item in files),
        "updated_at": updated_at_ms,
        "sha256": edge_function_summary_hash(files),
    }
    if include_files:
        payload["files"] = files
    return payload


def list_local_edge_functions(root, include_files=False):
    if not root.exists():
        raise RuntimeError(f"Edge functions directory does not exist: {root}")
    if not root.is_dir():
        raise RuntimeError(f"Edge functions path is not a directory: {root}")

    functions = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not NAME_RE.match(child.name):
            continue
        functions.append(local_edge_function_payload(root, child.name, include_files))
    return functions

def remote_functions_path(endpoint, suffix=""):
    ref = quote(endpoint["project_ref"], safe="")
    return f"/v1/projects/{ref}/functions{suffix}"


def list_remote_edge_functions(endpoint):
    payload = management_api_json(endpoint, remote_functions_path(endpoint))
    if isinstance(payload, dict) and isinstance(payload.get("functions"), list):
        return payload["functions"]
    if isinstance(payload, list):
        return payload
    raise RuntimeError("Unexpected edge functions list response")


def resolve_remote_edge_function(endpoint, identifier):
    functions = list_remote_edge_functions(endpoint)
    for function_item in functions:
        for field in ("slug", "name", "id"):
            if str(function_item.get(field)) == identifier:
                return function_item.get("slug") or function_item.get("name") or identifier
    return identifier


def get_remote_edge_function(endpoint, identifier):
    slug = resolve_remote_edge_function(endpoint, identifier)
    path = remote_functions_path(endpoint, f"/{quote(slug, safe='')}")
    return management_api_json(endpoint, path)


def parse_remote_edge_source(headers, raw, max_file_bytes=EDGE_SOURCE_MAX_FILE_BYTES):
    content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    if "multipart/" not in content_type:
        raise RuntimeError(f"Unexpected function body content type: {content_type}")

    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        + raw
    )
    if not message.is_multipart():
        raise RuntimeError("Function body response was not multipart")

    files = []
    metadata = {}
    for part in message.iter_parts():
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            files.append(decode_source_file(filename, payload, True, max_file_bytes))
            continue
        if payload:
            metadata = json.loads(payload.decode("utf-8"))

    files.sort(key=lambda item: item["path"])
    return metadata, files


def get_remote_edge_function_source(
    endpoint,
    identifier,
    max_file_bytes=EDGE_SOURCE_MAX_FILE_BYTES,
):
    detail = get_remote_edge_function(endpoint, identifier)
    slug = detail.get("slug") or detail.get("name") or identifier
    path = remote_functions_path(endpoint, f"/{quote(slug, safe='')}/body")
    _status, headers, raw = management_api_request(endpoint, path, "multipart/form-data")
    metadata, files = parse_remote_edge_source(headers, raw, max_file_bytes)
    return {
        "id": detail.get("id", slug),
        "slug": slug,
        "name": detail.get("name", slug),
        "status": detail.get("status"),
        "entrypoint_path": detail.get("entrypoint_path")
        or metadata.get("entrypoint_path")
        or metadata.get("deno2_entrypoint_path"),
        "import_map_path": detail.get("import_map_path") or metadata.get("import_map_path"),
        "verify_jwt": detail.get("verify_jwt"),
        "version": detail.get("version"),
        "created_at": detail.get("created_at"),
        "updated_at": detail.get("updated_at"),
        "ezbr_sha256": detail.get("ezbr_sha256"),
        "sha256": edge_function_summary_hash(files),
        "metadata": metadata,
        "file_count": len(files),
        "total_bytes": sum(file_item["size_bytes"] for file_item in files),
        "files": files,
    }


def edge_function_slug(function_item):
    return function_item.get("slug") or function_item.get("name") or function_item.get("id")


def edge_source_file_bytes(file_item):
    if "content" in file_item:
        return file_item["content"].encode("utf-8")
    if "content_base64" in file_item:
        return base64.b64decode(file_item["content_base64"])
    raise RuntimeError(f"Edge function source is missing content for {file_item.get('path')}")


def edge_function_relative_path(file_path):
    normalized = str(file_path).replace("\\", "/").strip().lstrip("/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if len(parts) >= 3 and parts[0] == "tmp" and parts[1].startswith("user_fn_"):
        parts = parts[2:]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Invalid edge function source path: {file_path}")
    return "/".join(parts)


def edge_function_archive_path(file_path):
    normalized = edge_function_relative_path(file_path)
    if normalized == "source":
        raise ValueError(f"Invalid edge function source path: {file_path}")
    if normalized.startswith("source/"):
        return normalized.removeprefix("source/")
    return normalized


def edge_function_metadata_path(file_path):
    normalized = str(file_path).replace("\\", "/").strip()
    if "/source/" in normalized:
        normalized = normalized.split("/source/", 1)[1]
    return edge_function_archive_path(normalized)


def write_edge_function_zip(function_item, archive_path):
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_item in sorted(function_item.get("files", []), key=lambda item: item["path"]):
            archive.writestr(
                edge_function_archive_path(file_item["path"]),
                edge_source_file_bytes(file_item),
            )


def multipart_form_data(parts):
    boundary = "----sync-api-" + uuid.uuid4().hex
    body = bytearray()
    for part in parts:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        disposition = f'Content-Disposition: form-data; name="{part["name"]}"'
        if part.get("filename"):
            disposition += f'; filename="{part["filename"]}"'
        body.extend((disposition + "\r\n").encode("utf-8"))
        if part.get("content_type"):
            body.extend(f'Content-Type: {part["content_type"]}\r\n'.encode("utf-8"))
        body.extend(b"\r\n")
        body.extend(part["content"])
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def edge_function_deploy_metadata(function_item):
    metadata = dict(function_item.get("metadata") or {})
    slug = edge_function_slug(function_item)
    metadata["name"] = slug
    for key in ("entrypoint_path", "import_map_path", "verify_jwt"):
        value = function_item.get(key)
        if value is not None:
            if key in ("entrypoint_path", "import_map_path"):
                metadata[key] = edge_function_metadata_path(value)
            else:
                metadata[key] = value
    return metadata


def edge_function_file_part(file_item):
    return {
        "name": "file",
        "filename": edge_function_archive_path(file_item["path"]),
        "content_type": "application/octet-stream",
        "content": edge_source_file_bytes(file_item),
    }


def deploy_remote_edge_function(endpoint, function_item):
    slug = edge_function_slug(function_item)
    if not slug:
        raise RuntimeError("Edge function source is missing a slug")

    metadata_raw = json.dumps(edge_function_deploy_metadata(function_item)).encode("utf-8")
    file_parts = [
        edge_function_file_part(file_item)
        for file_item in sorted(function_item.get("files", []), key=lambda item: item["path"])
    ]
    if not file_parts:
        raise RuntimeError(f"Edge function source is empty: {slug}")

    body, content_type = multipart_form_data(
        [
            {
                "name": "metadata",
                "content_type": "application/json",
                "content": metadata_raw,
            },
            *file_parts,
        ]
    )
    path = remote_functions_path(endpoint, f"/deploy?slug={quote(slug, safe='')}")
    status, _headers, raw = management_api_request(
        endpoint,
        path,
        accept="application/json",
        method="POST",
        body=body,
        content_type=content_type,
    )
    if status < 200 or status >= 300:
        detail = raw.decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase Management API returned status {status}: {detail}")


def delete_remote_edge_function(endpoint, slug):
    management_api_request(
        endpoint,
        remote_functions_path(endpoint, f"/{quote(slug, safe='')}"),
        method="DELETE",
    )


def local_edge_function_dir(root, slug):
    if not NAME_RE.match(slug):
        raise ValueError("edge function id must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
    function_dir = root / slug
    try:
        function_dir.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("edge function path escapes functions root") from exc
    return function_dir


def write_local_edge_function(root, function_item):
    slug = edge_function_slug(function_item)
    if not slug:
        raise RuntimeError("Edge function source is missing a slug")
    function_dir = local_edge_function_dir(root, slug)
    if function_dir.exists():
        shutil.rmtree(function_dir)
    function_dir.mkdir(parents=True, exist_ok=True)
    for file_item in function_item.get("files", []):
        relative_path = Path(file_item["path"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"Invalid edge function source path: {file_item['path']}")
        target_file = function_dir / relative_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(edge_source_file_bytes(file_item))


def delete_local_edge_function(root, slug):
    function_dir = local_edge_function_dir(root, slug)
    if function_dir.exists():
        shutil.rmtree(function_dir)


def edge_function_digest(function_item):
    return function_item.get("sha256")


def compare_edge_functions(source, target):
    if not source.get("available") or not target.get("available"):
        return {
            "function_count_delta": None,
            "functions_missing_in_target": [],
            "functions_missing_in_source": [],
            "function_source_differences": [],
        }

    source_functions = {
        item.get("slug") or item.get("name") or item.get("id"): item
        for item in source["functions"]
    }
    target_functions = {
        item.get("slug") or item.get("name") or item.get("id"): item
        for item in target["functions"]
    }
    source_slugs = set(source_functions)
    target_slugs = set(target_functions)

    differences = []
    for slug in sorted(source_slugs & target_slugs):
        source_digest = edge_function_digest(source_functions[slug])
        target_digest = edge_function_digest(target_functions[slug])
        if source_digest and target_digest and source_digest != target_digest:
            differences.append(
                {
                    "slug": slug,
                    "source_sha256": source_digest,
                    "target_sha256": target_digest,
                }
            )

    return {
        "function_count_delta": target["function_count"] - source["function_count"],
        "functions_missing_in_target": [
            {"slug": slug} for slug in sorted(source_slugs - target_slugs)
        ],
        "functions_missing_in_source": [
            {"slug": slug} for slug in sorted(target_slugs - source_slugs)
        ],
        "function_source_differences": differences,
    }


def collect_edge_functions_side(role, environment, config):
    endpoint = edge_functions_endpoint_from_config(config, role)
    if not endpoint:
        return {
            "role": role,
            "environment": environment,
            "available": False,
            "error": "edge function metadata is not configured for this side",
            "function_count": 0,
            "functions": [],
        }

    try:
        if endpoint["kind"] == "local":
            functions = list_local_edge_functions(endpoint["root"])
        else:
            functions = list_remote_edge_functions(endpoint)
        return {
            "role": role,
            "environment": environment,
            "available": True,
            "source_kind": endpoint["kind"],
            "function_count": len(functions),
            "functions": functions,
        }
    except Exception as exc:
        return {
            "role": role,
            "environment": environment,
            "available": False,
            "source_kind": endpoint.get("kind"),
            "error": str(exc),
            "function_count": 0,
            "functions": [],
        }


def get_edge_function_side(role, environment, config, identifier, include_source):
    endpoint = edge_functions_endpoint_from_config(config, role)
    if not endpoint:
        return {
            "role": role,
            "environment": environment,
            "available": False,
            "error": "edge function metadata is not configured for this side",
            "function": None,
        }

    try:
        if endpoint["kind"] == "local":
            function_item = local_edge_function_payload(
                endpoint["root"],
                identifier,
                include_files=include_source,
                include_content=include_source,
            )
        elif include_source:
            function_item = get_remote_edge_function_source(endpoint, identifier)
        else:
            function_item = get_remote_edge_function(endpoint, identifier)
        return {
            "role": role,
            "environment": environment,
            "available": True,
            "source_kind": endpoint["kind"],
            "function": function_item,
        }
    except Exception as exc:
        return {
            "role": role,
            "environment": environment,
            "available": False,
            "source_kind": endpoint.get("kind"),
            "error": str(exc),
            "function": None,
        }
