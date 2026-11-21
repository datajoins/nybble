# ============================================================
# Nybble - Dockerfile  (by DataJoins, https://datajoins.com)
#
# Two-stage build:
#   Stage 0 (builder) - Python: runs pipeline for Mode A baked-in DB
#   Stage 1 (server)  - nginx + Python/Flask: serves the app at port 80
#
# All rendering is done by Flask serving self-contained HTML pages.
# No Node.js, no npm, no build tools required at any stage.
#
# HOW TO USE
# ----------
# Mode A - Bake a database in at build time:
#   Place input.db / input.sqlite / input.sql / input.sql.gz in the repo root
#   docker build -f deploy/Dockerfile -t datajoins/nybble .
#   docker run -p 7700:80 datajoins/nybble
#
# Mode B - Volume-mount (database provided at container start):
#   docker run -p 7700:80 -v /path/to/my.db:/input/database:ro datajoins/nybble
#
# Mode C - Upload via the UI at http://localhost:7700
# ============================================================

# ── Stage 0: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full server package (preserves sub-package structure)
COPY server/ ./server/
# Copy nybble/core/ - the shared internal library imported by server/ingestion/ and server/kg/
# nybble.core has no Flask, DuckDB, or CLI deps - safe to include in the server image.
# nybble/__init__.py is NOT copied: the server only needs nybble.core, not the full CLI package.
# A minimal nybble/__init__.py is created below so Python treats nybble/ as a package.
COPY nybble/core/            ./nybble/core/
RUN printf '# nybble package stub - server image only exposes nybble.core\n' > ./nybble/__init__.py
COPY deploy/docker-entrypoint.sh ./docker-entrypoint.sh

RUN chmod +x docker-entrypoint.sh
RUN mkdir -p /input /output/erd

# ── Mode A: optional baked-in database ──────────────────────────────────────
# Create 0-byte sentinel files; glob patterns below only overwrite when a
# real input file exists in the build context.
RUN printf '' > /input/.keep && \
    cp /input/.keep /input/candidate_db && \
    cp /input/.keep /input/candidate_sql && \
    cp /input/.keep /input/candidate_gz

COPY input.d[b]      /input/candidate_db
COPY input.sqlit[e]  /input/candidate_db
COPY input.sqlite[3] /input/candidate_db
COPY input.sq[l]     /input/candidate_sql
COPY input.sql.g[z]  /input/candidate_gz

RUN for CAND in /input/candidate_db /input/candidate_sql /input/candidate_gz; do \
        if [ -s "${CAND}" ] && [ "$(wc -c < ${CAND})" -gt "$(wc -c < /input/.keep)" ]; then \
            echo "[ok] Mode A: building ERD from ${CAND}"; \
            KEY="$(sha256sum ${CAND} | cut -c1-16)"; \
            ERD_DIR="/output/erd/${KEY}"; \
            mkdir -p "${ERD_DIR}"; \
            python3 -m server.pipeline.run_pipeline "${CAND}" --output-dir "${ERD_DIR}" 2>&1 | tee /tmp/build.log; \
            printf '%s' "$(basename ${CAND})" > "${ERD_DIR}/.name"; \
            break; \
        fi; \
    done; \
    if [ -z "$(ls -A /output/erd/)" ]; then \
        echo "[info] No database baked in - Mode B/C active at runtime."; \
    fi

# ── Stage 1: server ───────────────────────────────────────────────────────────
# Use python:3.12-slim (Debian-based) instead of nginx:alpine so that pip
# packages from the builder stage copy cleanly (same glibc ABI, same site-packages
# path: /usr/local/lib/python3.12/site-packages).  nginx is installed via apt.
FROM python:3.12-slim AS server

RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx \
        ca-certificates \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Copy pip site-packages wholesale from the builder - same Python + glibc = works.
COPY --from=builder /usr/local/lib/python3.12/site-packages \
                    /usr/local/lib/python3.12/site-packages

RUN rm -rf /usr/share/nginx/html/*

# Copy the Python server package
COPY --from=builder /app/server/ /app/server/
# Copy nybble/core/ (shared internal library - no new pip deps introduced)
COPY --from=builder /app/nybble/ /app/nybble/

# Pre-built ERDs (empty in Mode B/C, populated in Mode A)
COPY --from=builder /output/erd /output/erd

COPY deploy/nginx.conf        /etc/nginx/conf.d/nybble.conf
RUN rm -f /etc/nginx/sites-enabled/default \
    && rm -f /etc/nginx/conf.d/default.conf \
    && ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log

COPY deploy/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

VOLUME ["/uploads", "/output"]
EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]

LABEL org.opencontainers.image.title="Nybble" \
      org.opencontainers.image.description="Ultra-lightweight data engine: browse, query, ERD, and Knowledge Graph from any source." \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.vendor="DataJoins" \
      org.opencontainers.image.url="https://datajoins.com" \
      org.opencontainers.image.source="https://github.com/datajoins/nybble" \
      org.opencontainers.image.licenses="Apache-2.0"
