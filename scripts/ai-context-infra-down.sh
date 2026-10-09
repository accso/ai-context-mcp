#!/usr/bin/env bash
# ai-context-infra-down.sh — Stop ALL shared, project-independent infrastructure:
# Qdrant + Postgres + the shared inference service (embeddings + re-ranking).
#
# This is the explicit counterpart to ai-context-infra-up.sh. Independent of
# per-project MCP servers/watchers — use `mise run ai-context-down` for those.
#
# Usage:
#   mise run ai-context-infra-down                  — stop infra (keep DB volumes)
#   mise run ai-context-infra-down -- --wipe        — also delete DB data volumes
#   mise run ai-context-infra-down -- --stop-services — stop per-project MCP/watchers first
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AI_CONTEXT_DIR="$ROOT_DIR/.ai-context"
INFRA_DIR="$AI_CONTEXT_DIR/infrastructure"

require_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERR: $1 not found. Install: $2"; exit 1; }; }
require_cmd docker "Install Rancher Desktop from rancherdesktop.io"

WIPE=false
STOP_SERVICES=false
for arg in "$@"; do
  [[ "$arg" == "--wipe"          ]] && WIPE=true
  [[ "$arg" == "--stop-services" ]] && STOP_SERVICES=true
done

# ── Optionally stop per-project MCP servers + watchers first ──────────────────
if [[ "$STOP_SERVICES" == "true" ]]; then
  echo "[ai-context-infra-down] Stopping per-project MCP servers and watchers first (--stop-services)..."
  bash "$ROOT_DIR/scripts/ai-context-down.sh" --no-backup || true
  echo ""
else
  RUNNING_SERVICES=$(docker ps --format "{{.Names}}" \
    | grep -E "^ai_context_(mcp|watcher)_" || true)
  if [[ -n "$RUNNING_SERVICES" ]]; then
    echo ""
    echo "  WARN: The following per-project services are still running and depend on this infra:"
    echo "$RUNNING_SERVICES" | sed 's/^/         /'
    echo ""
    echo "  Stop them first with:  mise run ai-context-down"
    echo "  Or re-run with:        bash scripts/ai-context-infra-down.sh --stop-services"
    echo ""
    read -r -p "  Continue stopping infra anyway? [y/N] " _CONFIRM
    case "${_CONFIRM:-N}" in
      [Yy]*) ;;
      *) echo "[ai-context-infra-down] Aborted."; exit 0 ;;
    esac
    echo ""
  fi
fi

# ── Stop shared services (qdrant, postgres, inference) ───────────────────────
if [[ "$WIPE" == "true" ]]; then
  echo "[ai-context-infra-down] Stopping services and WIPING all data volumes (--wipe)..."
  echo ""
  echo "  WARNING: This permanently deletes all indexed context data."
  echo "  Run  mise run ai-context-reset  afterwards to re-index from scratch."
  echo ""
  read -r -p "  Are you sure? [y/N] " _CONFIRM_WIPE
  case "${_CONFIRM_WIPE:-N}" in
    [Yy]*) ;;
    *) echo "[ai-context-infra-down] Aborted."; exit 0 ;;
  esac
  cd "$INFRA_DIR"
  docker compose down --volumes --remove-orphans 2>/dev/null || \
    docker stop ai_context_qdrant ai_context_postgres ai_context_inference 2>/dev/null || true
  echo ""
  echo "[ai-context-infra-down] Services stopped and volumes wiped."
else
  echo "[ai-context-infra-down] Stopping shared services (volumes preserved)..."
  bash "$INFRA_DIR/stop.sh"
  echo ""
  echo "[ai-context-infra-down] Done. Data volumes are intact."
  echo "  Restart with:  mise run ai-context-infra-up"
  echo "  Wipe data:     bash scripts/ai-context-infra-down.sh --wipe"
fi
echo ""
