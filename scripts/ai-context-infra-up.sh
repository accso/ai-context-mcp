#!/usr/bin/env bash
# ai-context-infra-up.sh — Ensure ALL shared, project-independent infrastructure
# is running: Qdrant + Postgres + the shared inference service (embeddings +
# re-ranking, see .ai-context/inference/).
#
# These live in their own "ai-context-infrastructure" Docker Compose project
# (.ai-context/infrastructure/docker-compose.yml) — fully separate from the
# "ai-context" namespace used by the per-project MCP server + watcher
# containers (managed by `mise run ai-context-up` / `ai-context-down`).
# Stopping the per-project layer must NEVER stop this infra layer, and vice versa.
#
# Starts whatever isn't already running; stops cleanly on Ctrl-C (only the
# pieces THIS invocation started — anything already running before is left up).
#
# By default this keeps running in the foreground (the "two persistent
# terminals" workflow) so Ctrl-C can stop what it started. Pass --daemon (or
# -d) to return control to your shell once startup finishes instead — the
# underlying containers are already daemonized by Docker either way; this
# flag only controls whether THIS SCRIPT stays attached to catch Ctrl-C.
# Stop a daemon-mode instance later with `mise run ai-context-infra-down`.
#
# Usage:
#   mise run ai-context-infra-up          — foreground, Ctrl-C to stop
#   mise run ai-context-infra-up-daemon   — start and return immediately
#   (equivalent to: ./ai-context-infra-up.sh --daemon)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AI_CONTEXT_DIR="$ROOT_DIR/.ai-context"
INFRA_DIR="$AI_CONTEXT_DIR/infrastructure"

require_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERR: $1 not found. Install: $2"; exit 1; }; }
require_cmd docker  "Install Rancher Desktop from rancherdesktop.io (or Docker if preferred)"

DAEMON=false
for arg in "$@"; do
  case "$arg" in
    --daemon|-d) DAEMON=true ;;
  esac
done

_CLEANED=0
_WE_STARTED_DB=false

cleanup() {
  [[ $_CLEANED -eq 1 ]] && return
  _CLEANED=1
  echo
  if [[ "$_WE_STARTED_DB" == "true" ]]; then
    echo "[ai-context-infra-up] stopping shared services..."
    bash "$INFRA_DIR/stop.sh" || true
  else
    echo "[ai-context-infra-up] nothing was started by this invocation — leaving infra as found."
  fi
}

int_handler() { cleanup; trap - EXIT; exit 0; }
trap cleanup EXIT
trap int_handler INT TERM

# ── Shared services (Qdrant + Postgres + Inference) ───────────────────────────
if docker ps --format "{{.Names}}" | grep -q "^ai_context_qdrant$"; then
  echo "[ai-context-infra-up] shared services are already running -- nothing to do."
  echo "   Qdrant:    http://localhost:6333"
  echo "   Postgres:  localhost:5433"
else
  echo "[ai-context-infra-up] shared services not running -- starting..."
  bash "$INFRA_DIR/start.sh"
  _WE_STARTED_DB=true
fi

echo ""
if [[ "$DAEMON" == "true" ]]; then
  # Detach: disable the cleanup trap so returning control to the shell (a
  # normal exit) doesn't tear down what was just started. Docker itself
  # already runs these containers detached regardless of this flag — this
  # only controls whether the SCRIPT stays attached to catch Ctrl-C.
  trap - EXIT INT TERM
  echo "[ai-context-infra-up] Infra ready (--daemon) -- returning control to your shell."
  echo "  Stop later with:  mise run ai-context-infra-down"
  exit 0
fi

echo "[ai-context-infra-up] Infra ready. Press Ctrl-C to stop what this invocation started."

while true; do
  sleep 5
done
