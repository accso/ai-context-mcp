#!/usr/bin/env bash
# ai-context-clean.sh - Completely remove this project from all AI Context databases.
# Removes the Qdrant collection and ALL Postgres data (including project registration).
# Use this when removing/archiving a project, not when re-indexing.
# For a fresh re-index while keeping the project registered, use: mise run ai-context-reset
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AI_CONTEXT_DIR="$ROOT_DIR/.ai-context"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERR: $1 not found. Install: $2"; exit 1; }
}
require_cmd docker "Install Rancher Desktop from rancherdesktop.io (or Docker if preferred)"

bash "$AI_CONTEXT_DIR/scripts/clean.sh" "${1:-}"

