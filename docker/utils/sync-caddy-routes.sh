#!/usr/bin/env bash

set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
docker_dir=$(CDPATH= cd -- "${script_dir}/.." && pwd)

caddyfile="${docker_dir}/volumes/proxy/caddy/Caddyfile"
instances_root="${docker_dir}/instances"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --caddyfile)
      caddyfile="${2:-}"
      shift 2
      ;;
    --instances-root)
      instances_root="${2:-}"
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Rebuild the generated-instance section of the shared Caddyfile.

Usage:
  ./utils/sync-caddy-routes.sh
  ./utils/sync-caddy-routes.sh --caddyfile /path/to/Caddyfile --instances-root /path/to/instances
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

begin_marker="# BEGIN GENERATED INSTANCE ROUTES"
end_marker="# END GENERATED INSTANCE ROUTES"

if ! grep -qxF "${begin_marker}" "${caddyfile}" || ! grep -qxF "${end_marker}" "${caddyfile}"; then
  {
    printf '\n%s\n%s\n' "${begin_marker}" "${end_marker}"
  } >> "${caddyfile}"
fi

generated_tmp=$(mktemp)
output_tmp=$(mktemp)
trap 'rm -f "${generated_tmp}" "${output_tmp}"' EXIT

{
  echo "# Managed by utils/sync-caddy-routes.sh"
  echo
  if [[ -d "${instances_root}" ]]; then
    while read -r fragment; do
      cat "${fragment}"
      echo
    done < <(find "${instances_root}" -mindepth 3 -maxdepth 3 -type f -name '*.caddy' | sort)
  fi
} > "${generated_tmp}"

awk -v begin="${begin_marker}" -v end="${end_marker}" -v generated="${generated_tmp}" '
  $0 == begin {
    print
    while ((getline line < generated) > 0) {
      print line
    }
    close(generated)
    skip = 1
    next
  }
  $0 == end {
    skip = 0
    print
    next
  }
  skip != 1 {
    print
  }
' "${caddyfile}" > "${output_tmp}"

mv "${output_tmp}" "${caddyfile}"
echo "Updated ${caddyfile}"
