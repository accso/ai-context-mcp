#!/usr/bin/env bash
# ai-context-reset.sh — Wipe ALL projects then re-init and restart.
#
# Flow:
#   1. Validate config + write .resolved.json
#   2. For each project with a backup: show info and ask restore/init upfront
#   3. Single overall "Are you sure?" confirmation
#   4. Clean every project non-interactively (stop containers + wipe DBs)
#   5. Hand off to ai-context-up.sh (reads per-project restore-choice marker files)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AI_CONTEXT_DIR="$ROOT_DIR/.ai-context"
RESOLVED="$AI_CONTEXT_DIR/.resolved.json"

require_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERR: $1 not found. Install: $2"; exit 1; }; }
require_cmd python3 "brew install python3"
require_cmd jq      "brew install jq"
require_cmd docker  "Install Rancher Desktop from rancherdesktop.io"

# ── Step 1: Validate config ───────────────────────────────────────────────────
echo "[ai-context-reset] Validating config..."
set +e
python3 "$AI_CONTEXT_DIR/scripts/validate_config.py" \
  --config    "$AI_CONTEXT_DIR/config.yaml" \
  --repo-root "$ROOT_DIR" \
  --output    "$RESOLVED"
VALIDATE_EXIT=$?
set -e
if [ "$VALIDATE_EXIT" -eq 1 ]; then
  echo "ERR Config validation failed. Fix the errors above and retry."
  exit 1
fi

PROJECT_KEYS=()
while IFS= read -r k; do PROJECT_KEYS+=("$k"); done \
  < <(jq -r '.projects | keys[]' "$RESOLVED")
PROJECT_COUNT=${#PROJECT_KEYS[@]}

echo ""
echo "[ai-context-reset] About to reset ALL $PROJECT_COUNT projects:"
while IFS= read -r k; do
  DISPLAY=$(jq -r ".projects[\"$k\"].display_name" "$RESOLVED")
  printf "    • %-40s (%s)\n" "$DISPLAY" "$k"
done < <(jq -r '.projects | keys[]' "$RESOLVED")
echo ""

# ── Step 2: Collect backup restore choices upfront ───────────────────────────
HAS_BACKUP_QUESTION=false
for PROJECT_KEY in "${PROJECT_KEYS[@]}"; do
  BACKUP_META="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/backup_meta.json"
  BACKUP_PG="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/postgres.json"
  BACKUP_SNAP="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/qdrant.snapshot"
  BACKUP_JSON="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/qdrant.vectors.json"
  RESTORE_CHOICE_FILE="${AI_CONTEXT_DIR}/.restore-choice-${PROJECT_KEY}"

  rm -f "$RESTORE_CHOICE_FILE"

  QDRANT_FILE_EXISTS=false
  [[ -f "$BACKUP_SNAP" ]] && QDRANT_FILE_EXISTS=true
  [[ -f "$BACKUP_JSON" ]] && QDRANT_FILE_EXISTS=true

  if [[ "$QDRANT_FILE_EXISTS" == "true" ]] && [[ -f "$BACKUP_PG" ]]; then
    HAS_BACKUP_QUESTION=true
    echo "  [backup] Backup found for '${PROJECT_KEY}':"
    if [[ -f "$BACKUP_META" ]]; then
      BACKUP_DATE=$(jq -r '.created_at' "$BACKUP_META" 2>/dev/null | cut -c1-19 | tr 'T' ' ' || echo "unknown")
      BACKUP_FILES=$(jq -r '.file_count'  "$BACKUP_META" 2>/dev/null || echo "?")
      BACKUP_CHUNKS=$(jq -r '.chunk_count' "$BACKUP_META" 2>/dev/null || echo "?")
      echo "           Created : ${BACKUP_DATE} UTC"
      echo "           Content : ${BACKUP_FILES} files, ${BACKUP_CHUNKS} chunks"
    else
      if [[ -f "$BACKUP_JSON" ]]; then
        echo "           JSON    : $(date -r "$BACKUP_JSON" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 'unknown') (file mtime)"
      fi
      if [[ -f "$BACKUP_SNAP" ]]; then
        echo "           Snapshot: $(date -r "$BACKUP_SNAP" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 'unknown') (file mtime)"
      fi
    fi
    echo "           (newer file will be used for restore)"
    echo ""
    read -r -p "  Restore '${PROJECT_KEY}' from backup instead of full re-index? [Y/n] " _RESTORE_CHOICE
    case "${_RESTORE_CHOICE:-Y}" in
      [Yy]*) echo "restore" > "$RESTORE_CHOICE_FILE" ;;
      *)     echo "init"    > "$RESTORE_CHOICE_FILE" ;;
    esac
    echo ""
  fi
done

[[ "$HAS_BACKUP_QUESTION" == "true" ]] && echo ""

# ── Step 3: Single overall confirmation ──────────────────────────────────────
read -r -p "    Reset ALL $PROJECT_COUNT projects? This wipes all data. [Y/n] " confirm
case "${confirm:-Y}" in
  [Yy]*) ;;
  *)
    echo "    Aborted."
    # Clean up any marker files we wrote
    for PROJECT_KEY in "${PROJECT_KEYS[@]}"; do
      rm -f "${AI_CONTEXT_DIR}/.restore-choice-${PROJECT_KEY}"
    done
    exit 0
    ;;
esac
echo ""

# ── Step 4: Clean all projects non-interactively ─────────────────────────────
for PROJECT_KEY in "${PROJECT_KEYS[@]}"; do
  bash "$AI_CONTEXT_DIR/scripts/clean.sh" --yes "$PROJECT_KEY"
  echo ""
done

# ── Step 5: Re-init and restart via ai-context-up ────────────────────────────
# Marker files written in step 2 tell ai-context-up.sh to restore or init each project.
exec bash "$ROOT_DIR/scripts/ai-context-up.sh"

