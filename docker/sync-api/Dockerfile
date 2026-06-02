FROM python:3.12-alpine

RUN apk add --no-cache \
  bash \
  coreutils \
  docker-cli \
  jq \
  perl \
  postgresql16-client

WORKDIR /app

COPY app.py /app/app.py
COPY sync_api /app/sync_api
COPY openapi.json /app/openapi.json
COPY branching.md /app/branching.md

ENV SYNC_API_HOST=0.0.0.0
ENV SYNC_API_PORT=8080
ENV SYNC_API_DATA_DIR=/data
ENV PROMOTE_MIGRATIONS_SCRIPT=/opt/sync-api/promote-migrations.sh

CMD ["python", "/app/app.py"]
