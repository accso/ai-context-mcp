#!/usr/bin/env bash
# ai-context-backup.sh — Create a backup for ALL projects in parallel.
#
# Usage:
#   mise run ai-context-backup               — both snapshot + json (default)
#   mise run ai-context-backup -- --snapshot — snapshot only (fast, local restore)
#   mise run ai-context-backup -- --json     — json only (git baseline update)
#
# Per project writes (depending on --format):
#   qdrant.snapshot      — local fast-restore (gitignored)
#   qdrant.vectors.json  — git baseline for teammates (commit this)
#   postgres.json        — row export (commit this)
#   backup_meta.json     — local display metadata (gitignored)
#
# After --both or --json, share with teammates:
#   git add .ai-context/database-backup/
#   git commit -m "update ai-context baseline"
#   git push
#
# Restore priority (newer file wins by mtime):
#   Local dev (after backup): snapshot is newer → fast restore, HNSW preserved
#   Fresh git clone (json only): json is used   → git-baseline restore
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AI_CONTEXT_DIR="$ROOT_DIR/.ai-context"
RESOLVED="$AI_CONTEXT_DIR/.resolved.json"

source "$AI_CONTEXT_DIR/scripts/lib/build_helpers.sh"
require_cmd python3 "brew install python3"
require_cmd jq      "brew install jq"
require_cmd docker  "Install Rancher Desktop from rancherdesktop.io"

# ── Parse format flag ─────────────────────────────────────────────────────────
# Explicit flag overrides config; if no flag is given the config default is used.
BACKUP_FORMAT_FLAG=""
for _arg in "$@"; do
  case "$_arg" in
    --snapshot|-s) BACKUP_FORMAT_FLAG="snapshot" ;;
    --json|-j)     BACKUP_FORMAT_FLAG="json"     ;;
    --both|-b)     BACKUP_FORMAT_FLAG="both"     ;;
  esac
done

# ── Validate config / ensure resolved.json exists ────────────────────────────
if [ ! -f "$RESOLVED" ]; then
  echo "[ai-context-backup] Validating config..."
  set +e
  python3 "$AI_CONTEXT_DIR/scripts/validate_config.py" \
    --config    "$AI_CONTEXT_DIR/config.yaml" \
    --repo-root "$ROOT_DIR" \
    --output    "$RESOLVED"
  VALIDATE_EXIT=$?
  set -e
  if [ "$VALIDATE_EXIT" -eq 1 ]; then
    echo "ERR Config validation failed."
    exit 1
  fi
fi

# ── Shared DB must be running ─────────────────────────────────────────────────
if ! docker ps --format "{{.Names}}" | grep -q "^ai_context_qdrant$"; then
  echo "ERR Shared DB stack not running. Run: mise run ai-context-infra-up"
  exit 1
fi

# ── Resolve final backup format (flag > config default) ──────────────────────
if [[ -n "$BACKUP_FORMAT_FLAG" ]]; then
  BACKUP_FORMAT="$BACKUP_FORMAT_FLAG"
else
  BACKUP_FORMAT=$(jq -r '.backup_format // "both"' "$RESOLVED")
fi
echo "[ai-context-backup] Format: $BACKUP_FORMAT"

# ── Build watcher image (always rebuild to pick up any code changes) ─────────
build_image ai-context-watcher:latest "$AI_CONTEXT_DIR/watcher/"
ui_line_reset  # build_image printed its own status line above

# ── Launch all projects in parallel ──────────────────────────────────────────
PIDS=()
KEYS=()
LOGS=()

while IFS= read -r PROJECT_KEY; do
  DISPLAY=$(jq -r ".projects[\"$PROJECT_KEY\"].display_name" "$RESOLVED")
  BACKUP_DIR="$(jq -r '.backup_dir' "$RESOLVED")/$PROJECT_KEY"

  # Only back up registered projects
  REGISTERED=$(docker exec ai_context_postgres psql -U ai_context -d ai_context \
    -tAc "SELECT 1 FROM projects WHERE project_key = '${PROJECT_KEY}';" 2>/dev/null || echo "")
  if [[ -z "$REGISTERED" ]]; then
    ui_line "[ai-context-backup]" "SKIP '${PROJECT_KEY}' — not registered (run ai-context-up first)."
    continue
  fi

  mkdir -p "$BACKUP_DIR"

  LOG_FILE="$(mktemp -t ai-context-backup.XXXXXX)"
  (
    docker run --rm \
      --name "ai_context_backup_${PROJECT_KEY}" \
      --label "com.docker.compose.project=ai-context" \
      --network ai-context-net \
      -v "${BACKUP_DIR}:/backup" \
      -v "${RESOLVED}:/app/.resolved.json:ro" \
      -e MODE=backup \
      -e BACKUP_FORMAT="$BACKUP_FORMAT" \
      -e PROJECT_KEY="$PROJECT_KEY" \
      -e QDRANT_URL=http://ai_context_qdrant:6333 \
      -e POSTGRES_URL=postgresql://ai_context:ai_context_local@ai_context_postgres:5432/ai_context \
      --entrypoint python \
      ai-context-watcher:latest \
      entrypoint.py
  ) >"$LOG_FILE" 2>&1 &

  PIDS+=($!)
  KEYS+=("$PROJECT_KEY")
  LOGS+=("$LOG_FILE")
done < <(jq -r '.projects | keys[]' "$RESOLVED")

if [ ${#PIDS[@]} -eq 0 ]; then
  ui_line "[ai-context-backup]" "No registered projects found — nothing to back up."
  exit 0
fi

# ── Wait for all backups (running in parallel) via one live dashboard row
#    per project — the container's own INFO logs (snapshot/export progress)
#    are captured to a log file and shown as a live "last line" per row
#    instead of all projects' output interleaving directly on the terminal.
ROW_LABELS=()
for KEY in "${KEYS[@]}"; do ROW_LABELS+=("backup: $KEY"); done
ui_begin "${ROW_LABELS[@]}"

WATCH_ARGS=()
for i in "${!PIDS[@]}"; do
  WATCH_ARGS+=("$i" "${PIDS[$i]}" "${LOGS[$i]}")
done
ui_watch_parallel "${WATCH_ARGS[@]}"
ui_end

FAILED=()
for i in "${!KEYS[@]}"; do
  if [ "${UI_PARALLEL_EXIT_CODES[$i]}" -ne 0 ]; then
    FAILED+=("${KEYS[$i]}")
    echo "ERR ${KEYS[$i]} failed — full log:"
    cat "${LOGS[$i]}"
  fi
  rm -f "${LOGS[$i]}"
done

echo ""
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "==> Backup failed for: ${FAILED[*]}"
  exit 1
fi

echo "==> Backup complete for all projects."
echo ""
if [[ "$BACKUP_FORMAT" == "both" || "$BACKUP_FORMAT" == "json" ]]; then
  BACKUP_BASE=$(jq -r '.backup_dir' "$RESOLVED")
  # Show path relative to repo root for readability
  REL_BACKUP=$(python3 -c "import os; print(os.path.relpath('$BACKUP_BASE', '$ROOT_DIR'))" 2>/dev/null || echo "$BACKUP_BASE")
  echo "    To share with teammates — commit the JSON files:"
  echo "    git add $REL_BACKUP/"
  echo "    git commit -m 'update ai-context baseline'"
  echo "    git push"
  echo ""
fi
echo "    (qdrant.snapshot and backup_meta.json are gitignored — local only)"

