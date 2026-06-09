FROM python:3.12-alpine

RUN apk add --no-cache \
  bash \
  coreutils \
  docker-cli \
  jq \
  perl \
  postgresql17-client

WORKDIR /app

COPY app.py /app/app.py
COPY sync_api /app/sync_api
COPY jrp-supabase-slim.json /app/jrp-supabase-slim.json
COPY branching.md /app/branching.md
COPY imports.md /app/imports.md

ENV SYNC_API_HOST=0.0.0.0
ENV SYNC_API_PORT=8080
ENV SYNC_API_DATA_DIR=/data
ENV PROMOTE_MIGRATIONS_SCRIPT=/opt/sync-api/promote-migrations.sh

CMD ["python", "/app/app.py"]
