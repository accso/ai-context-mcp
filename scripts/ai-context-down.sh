#!/usr/bin/env bash
# ai-context-down.sh — Stop all AI context MCP servers and watchers.
#
# This is the explicit counterpart to ai-context-up.sh.
# It can be called at any time — regardless of whether ai-context-up.sh is
# still running in another terminal.
#
# What is stopped:
#   • All ai_context_mcp_<project>     containers (MCP servers)
#   • All ai_context_watcher_<project> containers (file watchers)
#
# What is NOT stopped (managed independently, shared infra layer):
#   • ai_context_postgres  (shared DB — use  mise run ai-context-infra-down  to manage)
#   • ai_context_qdrant    (vector store  — same)
#   • ai_context_reranker  (shared re-ranker — same)
#
# Usage:
#   bash scripts/ai-context-down.sh               — stop + take snapshot backup
#   bash scripts/ai-context-down.sh --no-backup   — stop without backup
#   mise run ai-context-down                       — same via mise
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AI_CONTEXT_DIR="$ROOT_DIR/.ai-context"

NO_BACKUP=false
for arg in "$@"; do
  [[ "$arg" == "--no-backup" ]] && NO_BACKUP=true
done

# ── Stop all MCP servers and watchers ────────────────────────────────────────
echo "[ai-context-down] Stopping MCP servers and watchers..."
bash "$AI_CONTEXT_DIR/scripts/stop.sh"

# ── Snapshot backup (fast, local-only — keeps context fresh for next start) ──
if [[ "$NO_BACKUP" == "false" ]]; then
  echo ""
  echo "[ai-context-down] Taking full backup — snapshot + json (use --no-backup to skip)..."
  bash "$ROOT_DIR/scripts/ai-context-backup.sh" --both || {
    echo "[ai-context-down] WARN: backup failed — continuing anyway."
  }
else
  echo ""
  echo "[ai-context-down] Skipping backup (--no-backup)."
fi

echo ""
echo "[ai-context-down] Done."
echo ""
echo "  The shared infra (Postgres + Qdrant + re-ranker) is still running."
echo "  To also stop it:  mise run ai-context-infra-down"
echo ""

