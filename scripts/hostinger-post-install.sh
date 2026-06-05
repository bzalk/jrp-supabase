#!/usr/bin/env bash
set -Eeuo pipefail

cd /

exec > >(tee -a /post_install.log) 2>&1

# Required. Override in the Hostinger post-install script before running.
if [ -z "${BASE_DOMAIN:-}" ] && [ -n "${1:-}" ]; then
  BASE_DOMAIN="$1"
fi
: "${BASE_DOMAIN:?Set BASE_DOMAIN, for example BASE_DOMAIN=example.com}"
: "${SYNC_API_TOKEN:?Set SYNC_API_TOKEN from the control plane/UX before running the installer}"

if [ "${#SYNC_API_TOKEN}" -lt 32 ]; then
  echo "[jrp-hostinger-post-install] ERROR: SYNC_API_TOKEN must be at least 32 characters" >&2
  exit 1
fi
case "$SYNC_API_TOKEN" in
  *[[:space:]]*)
    echo "[jrp-hostinger-post-install] ERROR: SYNC_API_TOKEN must not contain whitespace" >&2
    exit 1
    ;;
esac

export BASE_DOMAIN
export SYNC_API_TOKEN
export LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-admin@${BASE_DOMAIN}}"
export JRP_REPO_URL="${JRP_REPO_URL:-https://github.com/bzalk/jrp-supabase.git}"
export JRP_REPO_BRANCH="${JRP_REPO_BRANCH:-main}"
export JRP_INSTALL_DIR="${JRP_INSTALL_DIR:-/opt/jrp-supabase}"

apt-get update
apt-get install -y curl ca-certificates

curl -fsSL "https://raw.githubusercontent.com/bzalk/jrp-supabase/${JRP_REPO_BRANCH}/scripts/install-vps.sh" \
  -o /tmp/jrp-install-vps.sh
chmod +x /tmp/jrp-install-vps.sh

/tmp/jrp-install-vps.sh
