ARG STUDIO_IMAGE=supabase/studio:2026.03.16-sha-5528817
FROM ${STUDIO_IMAGE}

COPY patch-studio-mcp.mjs /tmp/patch-studio-mcp.mjs
RUN node /tmp/patch-studio-mcp.mjs && rm /tmp/patch-studio-mcp.mjs
