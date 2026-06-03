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
```

The installer is safe to rerun. Existing generated secrets in `.env` are
preserved unless `FORCE_REGENERATE_SECRETS=true` is set.

After install:

```bash
cd /opt/jrp-supabase/docker
docker compose -f docker-compose.yml -f docker-compose.traefik.yml ps
```
