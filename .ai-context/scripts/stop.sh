#!/usr/bin/env bash
# stop.sh — Stop ALL AI context MCP servers and watchers for this repository.
# Backups are manual (mise run ai-context-backup) — watchers stop immediately.
#
# Does NOT touch the shared re-ranker or shared DB — those are independent
# infra, managed via `mise run ai-context-infra-down` (scripts/ai-context-infra-down.sh).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CONTEXT_DIR="$(dirname "$SCRIPT_DIR")"
REPO_SLUG=$(basename "$(dirname "$AI_CONTEXT_DIR")")
MCP_CONTAINER="${REPO_SLUG}_context_mcp"
WATCHER_CONTAINER="${REPO_SLUG}_context_watcher"
LSP_CONTAINER="${REPO_SLUG}_context_lsp"

echo "==> Stopping AI context services..."

# Legacy per-project container names (may be present during migration)
docker ps --format "{{.Names}}" \
  | grep -E "^ai_context_(mcp|watcher)_" \
  | xargs -r -P0 docker rm -f >/dev/null 2>&1 || true

# Consolidated per-repo containers for this repository
docker rm -f "$MCP_CONTAINER"     >/dev/null 2>&1 || true
docker rm -f "$WATCHER_CONTAINER" >/dev/null 2>&1 || true
docker rm -f "$LSP_CONTAINER"     >/dev/null 2>&1 || true

echo "==> AI context services stopped."
echo "    (shared DB + re-ranker are untouched — stop with: mise run ai-context-infra-down)"
