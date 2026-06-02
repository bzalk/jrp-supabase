from .settings import *
from .audit import *

def management_api_request(
    endpoint,
    path,
    accept="application/json",
    method="GET",
    body=None,
    content_type=None,
):
    token = endpoint.get("access_token")
    if not token:
        raise RuntimeError(
            "Supabase access token is required for hosted edge function metadata"
        )

    url = endpoint["api_base"] + path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "User-Agent": "sync-api/0.1",
    }
    if content_type:
        headers["Content-Type"] = content_type

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=STATS_TIMEOUT_SECONDS) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Supabase Management API returned {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Supabase Management API request failed: {exc}") from exc


def management_api_json(endpoint, path, method="GET", payload=None):
    body = None
    content_type = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        content_type = "application/json"

    status, _headers, raw = management_api_request(
        endpoint,
        path,
        method=method,
        body=body,
        content_type=content_type,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"Supabase Management API returned status {status}")
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))
