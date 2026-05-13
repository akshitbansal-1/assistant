#!/usr/bin/env bash
# Starts the full Daily Work Intelligence Agent stack:
#   PostgreSQL + Redis (via Docker Compose) → API server → Celery worker → Celery beat

set -euo pipefail

# ── colours ────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${CYAN}[start]${NC} $*"; }
ok()   { echo -e "${GREEN}[start]${NC} $*"; }
warn() { echo -e "${YELLOW}[start]${NC} $*"; }
err()  { echo -e "${RED}[start]${NC} $*" >&2; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# ── prerequisite checks ─────────────────────────────────
if ! command -v docker &>/dev/null; then
  err "Docker not found. Install Docker Desktop and try again."
  exit 1
fi

if [ ! -f ".venv/bin/python" ]; then
  err "Virtual environment not found. Run:"
  err "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if [ ! -f ".env" ]; then
  warn ".env not found — copying from .env.example"
  cp .env.example .env
  warn "Edit .env and set any required secrets, then re-run this script."
fi

# ── infrastructure ──────────────────────────────────────
log "Starting Redis…"
docker compose up -d

DATABASE_URL_VALUE="$(grep -E '^DATABASE_URL=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
if [[ "$DATABASE_URL_VALUE" == postgresql* ]]; then
  log "Waiting for PostgreSQL…"
  until pg_isready -h 127.0.0.1 -U postgres -q 2>/dev/null; do
    sleep 1
  done
  ok "PostgreSQL is ready."
else
  ok "Using SQLite database."
fi

log "Waiting for Redis…"
until docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; do
  sleep 1
done
ok "Redis is ready."

# ── process management ──────────────────────────────────
PIDS=()

cleanup() {
  echo ""
  log "Shutting down…"
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  ok "All processes stopped."
}

trap cleanup INT TERM

# ── start processes ─────────────────────────────────────
mkdir -p logs

log "Starting Celery worker…"
.venv/bin/celery \
  -A app.workers.celery_app.celery_app worker \
  --loglevel=info \
  --logfile=logs/celery-worker.log &
PIDS+=($!)

log "Starting Celery beat scheduler…"
.venv/bin/celery \
  -A app.workers.celery_app.celery_app beat \
  --loglevel=info \
  --logfile=logs/celery-beat.log &
PIDS+=($!)

log "Starting API server…"
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  🚀  http://localhost:3000${NC}"
echo -e "${BOLD}  📖  http://localhost:3000/docs${NC}"
echo -e "${BOLD}  Press Ctrl+C to stop all processes${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Run uvicorn in the foreground so its output stays in the terminal
exec .venv/bin/uvicorn app.main:app \
  --host "${APP_HOST:-0.0.0.0}" \
  --port "${APP_PORT:-3000}" \
  --reload
