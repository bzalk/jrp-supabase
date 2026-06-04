#!/usr/bin/env bash
set -Eeuo pipefail

exec > >(tee -a /post_install.log) 2>&1

# Required. Override in the Hostinger post-install script before running.
if [ -z "${BASE_DOMAIN:-}" ] && [ -n "${1:-}" ]; then
  BASE_DOMAIN="$1"
fi
: "${BASE_DOMAIN:?Set BASE_DOMAIN, for example BASE_DOMAIN=example.com}"

export BASE_DOMAIN
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
