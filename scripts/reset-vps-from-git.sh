#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${JRP_REPO_URL:-https://github.com/bzalk/jrp-supabase.git}"
REPO_BRANCH="${JRP_REPO_BRANCH:-main}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git ca-certificates

rm -rf /tmp/jrp-reset-bootstrap
git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" /tmp/jrp-reset-bootstrap
exec /tmp/jrp-reset-bootstrap/scripts/reset-vps.sh "$@"
