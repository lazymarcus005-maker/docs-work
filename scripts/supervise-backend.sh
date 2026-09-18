#!/bin/bash
# Keeps the backend API alive: restarts uvicorn within 1s whenever it dies.
# Stop deliberately with: kill $(cat data/backend-supervisor.pid) data/backend.pid
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo $$ > "$ROOT/data/backend-supervisor.pid"
while true; do
  echo "[supervisor] starting backend $(date)" >> "$ROOT/data/backend.log"
  "$ROOT/.venv/bin/uvicorn" app.main:create_app --factory --port 8000 \
    --app-dir "$ROOT/backend" >> "$ROOT/data/backend.log" 2>&1 &
  echo $! > "$ROOT/data/backend.pid"
  wait $!
  code=$?
  echo "[supervisor] backend exited ($code), restarting in 1s $(date)" >> "$ROOT/data/backend.log"
  sleep 1
done
