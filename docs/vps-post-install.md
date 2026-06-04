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

Hostinger post-install example:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

export BASE_DOMAIN="example.com"
export LETSENCRYPT_EMAIL="admin@example.com"
export PROJECT_NAME="Client Project"
export ORG_NAME="Jamrock Partners"

curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/hostinger-post-install.sh \
  | bash
```

Equivalent argument form:

```bash
curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/hostinger-post-install.sh \
  | bash -s -- example.com
```

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

Repo updates are single-branch only. The installer and repair script fetch
`JRP_REPO_BRANCH` explicitly and fast-forward to `origin/JRP_REPO_BRANCH`
without using `git pull`, so server-side Git pull settings cannot make the
script attempt to fast-forward multiple branches.

## Repair Traefik SSL

If DNS was delayed during initial install and Traefik is serving its default
self-signed certificate, rerun only the route/TLS layer:

```bash
curl -fsSL https://raw.githubusercontent.com/bzalk/jrp-supabase/main/scripts/repair-traefik-ssl.sh \
  | bash -s -- example.com
```

This does not reinstall Docker, does not regenerate `.env` secrets, and does
not recreate the database. It updates the local Git checkout, updates domain
values, verifies DNS, force recreates Traefik plus the containers that carry
Traefik labels, and waits for Let's Encrypt certificates. It also removes stale
Docker Compose recreate containers with names like `66afc95e6c6b_authelia`,
which can be left behind by an interrupted `up --force-recreate` and block the
next repair run. It recreates only the route containers with `--no-deps`, so a
separate unhealthy Supabase service such as analytics cannot block TLS repair.
It does not remove database containers or Docker volumes.

Optional repair overrides:

```bash
export JRP_INSTALL_DIR="/opt/jrp-supabase"
export UPDATE_REPO="true"
export VERIFY_DNS="true"
export VERIFY_HTTPS="true"
export ENABLE_UFW="true"
```

After install:

```bash
cd /opt/jrp-supabase/docker
docker compose -f docker-compose.yml -f docker-compose.traefik.yml ps
```
