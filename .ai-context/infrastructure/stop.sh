#!/usr/bin/env bash
# stop.sh — Stop the shared AI Context services (containers and data are preserved).
# Part of the "ai-context-infrastructure" compose project.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "==> Stopping AI Context shared services (qdrant, postgres, inference)..."
cd "$SCRIPT_DIR"
docker compose stop qdrant postgres inference
echo "    OK stopped. Containers and data volumes are preserved."
