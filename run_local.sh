#!/usr/bin/env bash
# Local build + run: frontend (npm) then FastAPI (uvicorn) on :8501
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8501
cd "$ROOT"
[ -f .env ] && { set -a; . ./.env; set +a; }

echo "==> Frontend build"
(cd application/web && npm install && npm run build)

echo "==> Playground build (/playground)"
(cd playground && npm install && npm run build)
rm -rf application/playground_web/dist && mkdir -p application/playground_web && cp -R playground/dist application/playground_web/dist

PIDS="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
[ -n "$PIDS" ] && { echo "==> Port ${PORT} in use — killing $PIDS"; kill $PIDS 2>/dev/null || true; sleep 1; }

export ENABLE_API_DOCS="${ENABLE_API_DOCS:-1}"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
echo "==> http://localhost:${PORT}"
exec "$PY" -m uvicorn application.server:app --host 0.0.0.0 --port "${PORT}" --no-server-header
