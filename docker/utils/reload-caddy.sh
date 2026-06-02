#!/usr/bin/env bash

set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
docker_dir=$(CDPATH= cd -- "${script_dir}/.." && pwd)

sync_first=1
timeout_seconds=60
container_name="supabase-caddy"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-sync)
      sync_first=0
      shift
      ;;
    --timeout)
      timeout_seconds="${2:-}"
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Validate and reload the shared Caddy container.

Usage:
  ./utils/reload-caddy.sh
  ./utils/reload-caddy.sh --skip-sync --timeout 90
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "${sync_first}" -eq 1 ]]; then
  "${script_dir}/sync-caddy-routes.sh"
fi

docker exec "${container_name}" caddy validate --config /etc/caddy/Caddyfile
docker exec "${container_name}" caddy reload --config /etc/caddy/Caddyfile

deadline=$((SECONDS + timeout_seconds))
while (( SECONDS < deadline )); do
  if docker exec "${container_name}" caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    echo "Caddy reload completed."
    exit 0
  fi
  sleep 2
done

echo "Timed out waiting for Caddy reload confirmation." >&2
exit 1
