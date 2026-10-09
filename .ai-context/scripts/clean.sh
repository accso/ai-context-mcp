#!/usr/bin/env bash
# clean.sh — Completely remove this project from all AI Context databases.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CONTEXT_DIR="$(dirname "$SCRIPT_DIR")"
RESOLVED="$AI_CONTEXT_DIR/.resolved.json"
REPO_SLUG=$(basename "$(dirname "$AI_CONTEXT_DIR")")
MCP_CONTAINER="${REPO_SLUG}_context_mcp"
WATCHER_CONTAINER="${REPO_SLUG}_context_watcher"
LSP_CONTAINER="${REPO_SLUG}_context_lsp"

require_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERR '$1' not found. Install: $2"; exit 1; }; }
require_cmd jq     "brew install jq"
require_cmd docker "Install Rancher Desktop from rancherdesktop.io"

if [ ! -f "$RESOLVED" ]; then
  echo "ERR .resolved.json not found. Run: mise run ai-context-up  (validates and writes it)"
  exit 1
fi

# ── Argument parsing ──────────────────────────────────────────────────────────
# --yes / -y  Skip "Are you sure?" and backup prompts (used by ai-context-reset.sh
#             which collects all choices upfront before running any clean).
AUTO_YES=false
POSITIONAL=""
for _arg in "$@"; do
  case "$_arg" in
    --yes|-y) AUTO_YES=true ;;
    *)        POSITIONAL="$_arg" ;;
  esac
done

PROJECT_KEY="${POSITIONAL:-$(jq -r '.projects | keys[0]' "$RESOLVED")}"
DISPLAY_NAME=$(jq -r ".projects[\"$PROJECT_KEY\"].display_name" "$RESOLVED")
REPO_ROOT=$(jq   -r '.repo_root'                                "$RESOLVED")

echo "==> Cleaning AI Context for: $DISPLAY_NAME ($PROJECT_KEY)"
echo "    This will remove ALL data for this project from Qdrant and Postgres."
echo "    Use reset to re-index instead of clean if you want to keep the project registered."
echo ""

if [[ "$AUTO_YES" == "false" ]]; then
  read -r -p "    Are you sure? [Y/n] " confirm
  case "${confirm:-Y}" in
    [Yy]*) ;;
    *) echo "    Aborted."; exit 0 ;;
  esac
  echo ""

  # ── Check for available backup and ask (interactive / single-project flow) ──
  BACKUP_META="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/backup_meta.json"
  BACKUP_PG="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/postgres.json"
  BACKUP_SNAP="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/qdrant.snapshot"
  BACKUP_JSON="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/qdrant.vectors.json"
  RESTORE_CHOICE_FILE="${AI_CONTEXT_DIR}/.restore-choice-${PROJECT_KEY}"

  QDRANT_FILE_EXISTS=false
  [[ -f "$BACKUP_SNAP" ]] && QDRANT_FILE_EXISTS=true
  [[ -f "$BACKUP_JSON" ]] && QDRANT_FILE_EXISTS=true

  rm -f "$RESTORE_CHOICE_FILE"

  if [[ "$QDRANT_FILE_EXISTS" == "true" ]] && [[ -f "$BACKUP_PG" ]]; then
    echo "  [backup] Backup found for '${PROJECT_KEY}':"
    if [[ -f "$BACKUP_META" ]]; then
      BACKUP_DATE=$(jq -r '.created_at' "$BACKUP_META" 2>/dev/null | cut -c1-19 | tr 'T' ' ' || echo "unknown")
      BACKUP_FILES=$(jq -r '.file_count'  "$BACKUP_META" 2>/dev/null || echo "?")
      BACKUP_CHUNKS=$(jq -r '.chunk_count' "$BACKUP_META" 2>/dev/null || echo "?")
      echo "           Created : ${BACKUP_DATE} UTC"
      echo "           Content : ${BACKUP_FILES} files, ${BACKUP_CHUNKS} chunks"
    else
      # No local meta (e.g. fresh git clone) — show file mtime instead
      if [[ -f "$BACKUP_JSON" ]]; then
        BACKUP_DATE=$(date -r "$BACKUP_JSON" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
        echo "           JSON    : ${BACKUP_DATE} (file mtime, no backup_meta.json)"
      fi
      if [[ -f "$BACKUP_SNAP" ]]; then
        BACKUP_DATE=$(date -r "$BACKUP_SNAP" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
        echo "           Snapshot: ${BACKUP_DATE} (file mtime, no backup_meta.json)"
      fi
    fi
    echo ""
    read -r -p "  Restore '${PROJECT_KEY}' from backup after clean instead of full re-index? [Y/n] " _RESTORE_CHOICE
    case "${_RESTORE_CHOICE:-Y}" in
      [Yy]*) echo "restore" > "$RESTORE_CHOICE_FILE" ;;
      *)     echo "init"    > "$RESTORE_CHOICE_FILE" ;;
    esac
    echo ""
  fi
fi
# When AUTO_YES=true the restore-choice marker file was already written by the
# calling reset script — nothing to do here.

# ── Guard: shared DB must be running ──────────────────────────────────────────
if ! docker ps --format "{{.Names}}" | grep -q "^ai_context_qdrant$"; then
  echo "ERR Shared DB stack not running. Run: mise run ai-context-infra-up"
  exit 1
fi

# ── Stop consolidated containers for this repo (not project-specific) ────────
if docker ps --format "{{.Names}}" | grep -qE "^${MCP_CONTAINER}$|^${WATCHER_CONTAINER}$|^${LSP_CONTAINER}$"; then
  echo "--> Stopping consolidated AI context containers..."
  docker stop "$MCP_CONTAINER"      2>/dev/null || true
  docker rm   "$MCP_CONTAINER"      2>/dev/null || true
  docker stop "$WATCHER_CONTAINER"  2>/dev/null || true
  docker rm   "$WATCHER_CONTAINER"  2>/dev/null || true
  docker stop "$LSP_CONTAINER"      2>/dev/null || true
  docker rm   "$LSP_CONTAINER"      2>/dev/null || true
  echo "    OK stopped."
  echo ""
fi

# ── Wipe Qdrant collection ────────────────────────────────────────────────────
echo "--> Dropping Qdrant collection: $PROJECT_KEY ..."
HTTP_STATUS=$(docker run --rm --network ai-context-net curlimages/curl:latest \
  -s -o /dev/null -w "%{http_code}" -X DELETE \
  "http://ai_context_qdrant:6333/collections/${PROJECT_KEY}")
if [ "$HTTP_STATUS" = "200" ] || [ "$HTTP_STATUS" = "404" ]; then
  echo "    OK Qdrant collection dropped (or did not exist)."
else
  echo "ERR Qdrant DELETE returned HTTP $HTTP_STATUS"; exit 1
fi

# ── Wipe Postgres: delete project row (cascades to all related tables) ────────
echo "--> Removing project from Postgres: $PROJECT_KEY ..."
docker exec ai_context_postgres psql -U ai_context -d ai_context -c \
  "DELETE FROM projects WHERE project_key = '${PROJECT_KEY}';" > /dev/null
echo "    OK project removed from Postgres."

echo ""
echo "==> Clean complete for: $DISPLAY_NAME"
echo "    To re-add: mise run ai-context-up  (will re-init automatically)"
echo "    To clean another project: bash .ai-context/scripts/clean.sh <project-key>"
