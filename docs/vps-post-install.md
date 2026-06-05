# VPS Post-Install Bootstrap

Use a small provider post-install script that downloads the real installer from
this repository. The installer sets up Docker, the Supabase stack, the Sync API,
and an included Traefik reverse proxy.

Public bootstrap URL:

```text
https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/hostinger-post-install.sh
```

Public full installer URL:

```text
https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/install-vps.sh
```

Public Traefik SSL repair URL:

```text
https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/repair-traefik-ssl.sh
```

Public destructive reset URL:

```text
https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/reset-vps.sh
```

Public repair log endpoints on each installed Sync API:

```text
GET /v1/repair-logs
GET /v1/repair-logs/latest
GET /v1/repair-logs/{name}
```

Edge-function SSH integration notes:

```text
docs/edge-vps-operations.md
```

Hostinger post-install example:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

export BASE_DOMAIN="example.com"
export LETSENCRYPT_EMAIL="admin@example.com"
export PROJECT_NAME="Client Project"
export ORG_NAME="Jamrock Partners"
export SYNC_API_TOKEN="<control-plane-generated-token>"

curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/hostinger-post-install.sh \
  | bash
```

Equivalent argument form:

```bash
curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/hostinger-post-install.sh \
  | SYNC_API_TOKEN="<control-plane-generated-token>" bash -s -- example.com
```

`SYNC_API_TOKEN` is required for every install. The control plane/UX should
generate a strong random token, store it with the local environment record, and
pass the same value to the VPS installer. The installer writes that exact value
to `/opt/jrp-supabase/docker/.env`, replacing any previous Sync API token. The
installer does not generate or preserve this token locally.
Protected Sync API calls must use:

```http
Authorization: Bearer <SYNC_API_TOKEN>
```

## Reset And Try Again

Use this only when the VPS should be treated like a scratch install. It removes
the local checkout, stack containers, project Docker volumes, local bind-mounted
data under `/opt/jrp-supabase`, and generated local secrets, then downloads and
runs the normal installer again.

```bash
curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/reset-vps.sh \
  | CONFIRM_RESET=CONFIRM SYNC_API_TOKEN="<control-plane-generated-token>" bash -s -- example.com
```

To wipe without reinstalling:

```bash
curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/reset-vps.sh \
  | CONFIRM_RESET=CONFIRM RUN_INSTALL=false bash -s -- example.com
```

Equivalent CLI-argument form:

```bash
curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/reset-vps.sh \
  | SYNC_API_TOKEN="<control-plane-generated-token>" bash -s -- example.com --confirm CONFIRM
```

Smoke-test the reset flow without removing anything:

```bash
curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/reset-vps.sh \
  | SYNC_API_TOKEN="<control-plane-generated-token>" bash -s -- example.com --confirm CONFIRM --dry-run
```

GitHub is used only as a read-only source for downloading scripts and cloning
the public repo into the VPS. The install/reset scripts do not push to GitHub.
The reset and install scripts force their working directory to `/` before
deleting or recreating `/opt/jrp-supabase`, so a shell started inside the old
checkout cannot break the reinstall with `getcwd` errors.
The reset script also updates `/root/jrp-reset-vps-latest.log` so long-running
SSH resets can be started in the background and polled without waiting for the
HTTP request to stay open.

When `RUN_INSTALL=true`, reset also requires a fresh `SYNC_API_TOKEN` before it
removes the existing install. This keeps the reinstalled VPS aligned with the
new token stored by the control plane.

DNS records should point at the VPS before install:

```text
supabase.example.com
studio.example.com
auth.example.com
sync-api.example.com
```

Optional overrides:

```bash
export API_DOMAIN="api.example.com"
export STUDIO_DOMAIN="studio.example.com"
export AUTH_DOMAIN="auth.example.com"
export SYNC_API_DOMAIN="sync.example.com"
export JRP_INSTALL_DIR="/opt/jrp-supabase"
export SYNC_API_TOKEN="<control-plane-generated-token>"
export START_STACK="true"
export ENABLE_UFW="true"
export FORCE_REGENERATE_SECRETS="false"
export VERIFY_DNS="true"
export VERIFY_HTTPS="true"
```

The installer is safe to rerun. Existing generated secrets in `.env` are
preserved unless `FORCE_REGENERATE_SECRETS=true` is set.

The installer verifies that rendered Traefik labels contain the requested
domains, checks that DNS points to the VPS before starting TLS, and waits for
Let's Encrypt certificates. If DNS is intentionally not ready yet, set
`VERIFY_DNS=false` and `VERIFY_HTTPS=false`, then rerun the installer after DNS
is corrected.

Repo updates are single-branch only. The installer fetches only
`refs/heads/JRP_REPO_BRANCH` into `FETCH_HEAD` using an empty Git refmap, then
fast-forwards from there without using `git pull` or rewriting `origin/main`. A
local lock prevents overlapping install runs from updating the same checkout at
the same time. If the checkout updates while a public bootstrap script is
running, the installer re-runs itself from the updated local checkout before
continuing.

## Repair Traefik SSL

If DNS was delayed during initial install and Traefik is serving its default
self-signed certificate, rerun only the route/TLS layer:

```bash
curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/repair-traefik-ssl.sh \
  | bash -s -- example.com
```

This does not reinstall Docker, does not regenerate `.env` secrets, and does
not recreate the database. It does not update the local Git checkout by default;
the public `curl` command already downloads the latest repair script. It updates
domain values, verifies DNS, force recreates Traefik plus the containers that
carry Traefik labels, and waits for Let's Encrypt certificates. It also removes
stale Docker Compose recreate containers with names like
`66afc95e6c6b_authelia`, which can be left behind by an interrupted
`up --force-recreate` and block the next repair run. The repair script also
removes exact route-container name conflicts such as `authelia` after Compose
cleanup, then recreates only the route containers with `--no-deps`, so a
separate unhealthy Supabase service such as analytics cannot block TLS repair.
It waits until Docker fully releases those route container names before creating
replacements. A Docker Compose stack lock prevents overlapping install and
repair runs from recreating the same route containers at the same time. It does
not remove database containers or Docker volumes.

Every repair run writes a full log to:

```text
/opt/jrp-supabase/docker/repair-logs/
```

The log includes a preflight status report before the script mutates Docker
state and a failure status report if the repair exits non-zero. When Sync API is
reachable, the UX can link to:

```text
https://sync-api.example.com/v1/repair-logs/latest
```

Optional repair overrides:

```bash
export JRP_INSTALL_DIR="/opt/jrp-supabase"
export UPDATE_REPO="false"
export VERIFY_DNS="true"
export VERIFY_HTTPS="true"
export ENABLE_UFW="true"
export REPAIR_STATUS_ONLY="false"
```

Set `UPDATE_REPO=true` only when you intentionally want the repair run to pull
new stack files into that VPS checkout before repairing TLS.

Set `REPAIR_STATUS_ONLY=true` to collect the same preflight status report and
write the log without stopping, removing, or recreating any containers.

After install:

```bash
cd /opt/jrp-supabase/docker
docker compose -f docker-compose.yml -f docker-compose.traefik.yml ps
```
