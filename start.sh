#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
(cd frontend && npm install && npm run build)
cd backend
uv sync
echo
echo "GraphFlow: http://127.0.0.1:8000"
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
