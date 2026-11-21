#!/usr/bin/env sh
# deploy/docker-entrypoint.sh - Nybble container startup
#
# Starts the Nybble server:
#   1. Ensures output / upload directories exist.
#   2. If a volume-mounted file is present at /input/database (Mode B),
#      processes it immediately so the ERD is ready when the UI opens.
#   3. Starts the Flask API server in the background.
#   4. Waits for the Flask API to be ready (max 15 seconds).
#   5. Starts nginx in the foreground.

set -eu

UPLOAD_DIR="/uploads"
OUTPUT_DIR="/output/erd"
REGISTRY="/output/registry.json"
APP_DIR="/app"

mkdir -p "${UPLOAD_DIR}" "${OUTPUT_DIR}" "/output"

# ── Mode B: volume-mounted file ──────────────────────────────────────────────
# If /input/database exists and no ERD is baked for it yet, run the pipeline
# now (synchronously) so the ERD is available as soon as the UI opens.
if [ -f "/input/database" ]; then
    key="$(sha256sum /input/database | cut -c1-16)"
    erd_dir="${OUTPUT_DIR}/${key}"
    if [ ! -f "${erd_dir}/schema.json" ]; then
        echo "[ok] Mode B: running pipeline for /input/database (key=${key})"
        mkdir -p "${erd_dir}"
        PYTHONPATH="${APP_DIR}" python3 -m server.pipeline.run_pipeline /input/database \
            --output-dir "${erd_dir}" 2>&1 | tee /tmp/pipeline_boot.log
        printf '%s' "$(basename /input/database)" > "${erd_dir}/.name"
        echo "[ok] Mode B pipeline complete."
    else
        echo "[ok] Mode B: ERD already cached for /input/database (key=${key})"
    fi
fi

# ── Flask API server ─────────────────────────────────────────────────────────
echo "[ok] Starting Flask API server …"
cd "${APP_DIR}"
PYTHONPATH="${APP_DIR}" python3 -m server.app.api_server &
FLASK_PID=$!

# Wait until Flask is accepting connections (max 15 seconds)
for i in $(seq 1 30); do
    if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/files')" 2>/dev/null; then
        echo "[ok] Flask API ready."
        break
    fi
    sleep 0.5
done

# ── Nginx ────────────────────────────────────────────────────────────────────
echo "[ok] Starting nginx …"
exec nginx -g "daemon off;"
