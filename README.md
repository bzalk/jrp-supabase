# JRP Supabase Control Plane

This repository contains the deployable self-hosted Supabase stack used by JRP, plus a custom Python sync API for environment comparison, migration promotion, local branching, and MCP branch tools.

It intentionally does not contain the full upstream Supabase monorepo. The deployable surface lives under `docker/`.

## What Is Included

- `docker/docker-compose.yml` - self-hosted Supabase stack with JRP additions
- `docker/sync-api/` - Python API for environments, migrations, branches, OpenAPI, and branching docs
- `docker/studio-mcp/` - local Studio image patch that exposes JRP MCP tools
- `docker/utils/` - deployment, migration, branch, and proxy helper scripts
- `docker/volumes/` - static Supabase config files required by the compose stack
- `docker/.env.example` - sanitized environment template

## What Is Not Included

Runtime state is intentionally excluded from Git:

- `docker/.env`
- `docker/branches/`
- `docker/exported-migrations/`
- `docker/instances/`
- `docker/volumes/db/data/`
- `docker/volumes/storage/`
- `docker/volumes/authelia/`

## Local Start

```bash
cd docker
cp .env.example .env
# edit .env with real secrets and hostnames
docker compose up -d --build
```

## Tests

```bash
python3 -m unittest docker/sync-api/test_app.py
```

## Public Docs

The sync API serves:

- `/openapi.json`
- `/branching.md`

Those documents describe the branch, migration, and MCP-facing APIs exposed by the control plane.
