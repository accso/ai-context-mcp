#!/usr/bin/env bash
# ai-context-up.sh — Start the AI context layer for ALL projects; stop on Ctrl-C.
#
# Flow:
#   1. Validate config → write .resolved.json
#   2. For each project that is not yet initialised, restore from backup or run full init
#   3. Start consolidated MCP server + watcher containers (one each for ALL projects)
#   4. Register IDE configs (idempotent)
#   5. Print summary table (MCP URLs, watchers, log commands)
#   6. Keep running — Ctrl-C stops all containers gracefully
#
# By default this keeps running in the foreground (the "two persistent
# terminals" workflow) so Ctrl-C can stop everything cleanly. Pass --daemon
# (or -d) to return control to your shell once startup finishes instead — the
# containers themselves are already daemonized by Docker either way; this
# flag only controls whether THIS SCRIPT stays attached to catch Ctrl-C.
# Stop a daemon-mode instance later with `mise run ai-context-down`.
#
# Usage:
#   mise run ai-context-up          — foreground, Ctrl-C to stop
#   mise run ai-context-up-daemon   — start and return immediately
#   (equivalent to: ./ai-context-up.sh --daemon)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AI_CONTEXT_DIR="$ROOT_DIR/.ai-context"
RESOLVED="$AI_CONTEXT_DIR/.resolved.json"

source "$AI_CONTEXT_DIR/scripts/lib/ui.sh"
require_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERR: $1 not found. Install: $2"; exit 1; }; }
require_cmd python3 "brew install python3  (needed for config validator)"
require_cmd jq      "brew install jq"
require_cmd docker  "Install Rancher Desktop from rancherdesktop.io"

DAEMON=false
for arg in "$@"; do
  case "$arg" in
    --daemon|-d) DAEMON=true ;;
  esac
done

# ── Ctrl-C / EXIT handler ─────────────────────────────────────────────────────
_CLEANED=0
cleanup() {
  [[ $_CLEANED -eq 1 ]] && return
  _CLEANED=1
  echo ""
  echo "[ai-context-up] Ctrl-C received — stopping all AI context services..."
  echo ""
  bash "$AI_CONTEXT_DIR/scripts/stop.sh" || true
  echo ""
  echo "[ai-context-up] Running shutdown backup (snapshot + json, parallel)..."
  bash "$ROOT_DIR/scripts/ai-context-backup.sh" --both || true
}
int_handler() { cleanup; trap - EXIT; exit 0; }
trap cleanup    EXIT
trap int_handler INT TERM

# ── Step 1: Validate config + write .resolved.json ────────────────────────────
echo "[ai-context-up] Validating config..."
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
# exit code 2 = warnings only — safe to proceed

# ── Step 1b: Interactive Java/EPL-2.0 opt-in prompt ───────────────────────────
# validate_config.py flags any project with Java files that hasn't opted in
# (java_epl_pending=true in .resolved.json — see "Licensing — Java / EPL-2.0"
# in docs/lsp-integration.md). Java is deliberately NOT auto-enabled like
# every other language: enabling it downloads and runs the EPL-2.0-licensed
# Eclipse JDT Language Server directly from Red Hat's servers onto THIS
# machine, so that must be the deploying user's own explicit choice, made
# here interactively — not something baked into any image we publish, and
# not something this script decides silently on the user's behalf.
#
# Accepting writes a marker file (mirrors the existing
# `.restore-choice-<project>` convention) instead of programmatically
# rewriting the heavily-commented config.yaml, then re-validates so
# .resolved.json (and the LSP_LANGUAGES Docker build-arg derived from it)
# reflects the change before any project is initialised below.
JAVA_PENDING_PROJECTS=$(jq -r '.projects | to_entries[] | select(.value.lsp.java_epl_pending == true) | .key' "$RESOLVED")
if [[ -n "$JAVA_PENDING_PROJECTS" ]]; then
  JAVA_ACCEPTED_ANY=false
  while IFS= read -r PROJECT_KEY; do
    [[ -z "$PROJECT_KEY" ]] && continue
    echo ""
    echo "  [java] Java files detected in '${PROJECT_KEY}' — Java code intelligence is"
    echo "         currently disabled. Enabling it downloads and runs the EPL-2.0-"
    echo "         licensed Eclipse JDT Language Server directly from Red Hat's"
    echo "         servers onto this machine at Docker-build time (not bundled by"
    echo "         this project by default). See docs/lsp-integration.md for the"
    echo "         full licensing analysis before enabling."
    echo ""
    # Only prompt if stdin is a terminal (non-interactive CI environments
    # always leave Java disabled — same "safe default" as the restore prompt
    # above, but defaulting to N here since this opts into a non-permissive
    # license rather than a low-risk convenience choice).
    if [ -t 0 ]; then
      read -r -p "  Enable Java code intelligence for '${PROJECT_KEY}'? [y/N] " _JAVA_CHOICE
    else
      _JAVA_CHOICE="n"
      echo "  (non-interactive mode — leaving Java disabled)"
    fi
    case "${_JAVA_CHOICE:-N}" in
      [Yy]*)
        touch "$AI_CONTEXT_DIR/.java-epl-accepted-${PROJECT_KEY}"
        JAVA_ACCEPTED_ANY=true
        echo "  Enabled for '${PROJECT_KEY}' — this choice is remembered for future runs."
        ;;
      *)
        echo "  Leaving Java disabled for '${PROJECT_KEY}'."
        ;;
    esac
  done <<< "$JAVA_PENDING_PROJECTS"

  if [[ "$JAVA_ACCEPTED_ANY" == "true" ]]; then
    echo ""
    echo "[ai-context-up] Re-validating config after Java opt-in..."
    set +e
    python3 "$AI_CONTEXT_DIR/scripts/validate_config.py" \
      --config    "$AI_CONTEXT_DIR/config.yaml" \
      --repo-root "$ROOT_DIR" \
      --output    "$RESOLVED"
    VALIDATE_EXIT=$?
    set -e
    if [ "$VALIDATE_EXIT" -eq 1 ]; then
      echo "ERR Config validation failed after Java opt-in. Fix the errors above and retry."
      exit 1
    fi
  fi
fi

# ── Step 2: Ensure shared infra is running ────────────────────────────────────
# Auto-start the shared DB stack (+ re-ranker) if it isn't up yet, so users
# don't have to know the two-command startup order. ai-context-infra-up.sh is
# fully idempotent (it checks `docker ps` itself and no-ops if already
# running), so it's safe to call unconditionally here. Always pass --daemon:
# this call must RETURN so this script can continue to the next step —
# without it, ai-context-infra-up.sh's own foreground keep-alive loop would
# hang this script forever the first time infra needs auto-starting.
if ! docker ps --format "{{.Names}}" | grep -q "^ai_context_qdrant$"; then
  echo "[ai-context-up] Shared infra not running -- starting it now..."
  bash "$ROOT_DIR/scripts/ai-context-infra-up.sh" --daemon
fi

# NOTE: images are NOT pre-built here on purpose (previously this step ran an
# unconditional `docker build -q` for watcher+mcp before init/restore, whose
# only visible effect was a noisy pair of sha256 lines). It was redundant:
# init.sh/restore.sh below already build-if-missing before running a one-off
# container, and start.sh (step 6) unconditionally rebuilds all three images
# from source anyway, with a proper live status dashboard. Removing this
# avoids doing (and re-doing) the same build twice per run.

# ── Step 4: Check required models are available before any destructive init ─────
# If this is a first start (no projects registered) and a model is misconfigured
# or not loaded, the initial full index would overwrite any vector database that
# was restored from infra startup. Query the provider's /v1/models endpoint and
# abort early with a clear warning instead.
ONE_NEEDS_INIT=false
while IFS= read -r PROJECT_KEY; do
  REGISTERED=$(docker exec ai_context_postgres psql -U ai_context -d ai_context \
    -tAc "SELECT 1 FROM projects WHERE project_key = '${PROJECT_KEY}';" 2>/dev/null || echo "")
  if [[ -z "$REGISTERED" ]]; then
    ONE_NEEDS_INIT=true
    break
  fi
done < <(jq -r '.projects | keys[]' "$RESOLVED")

if [[ "$ONE_NEEDS_INIT" == "true" ]]; then
  if ! ui_task "Checking configured models are available" -- \
       python3 "$AI_CONTEXT_DIR/scripts/check_models.py" --resolved "$RESOLVED"; then
    echo ""
    echo "ERR Required model(s) are not available. Load the missing model(s) and retry."
    exit 2
  fi
fi

# ── Step 5: Per-project init / restore (blocking) ─────────────────────────────
# Initialisation runs sequentially so the Postgres/Qdrant state is consistent
# before the consolidated watcher starts observing all project roots.
while IFS= read -r PROJECT_KEY; do
  REGISTERED=$(docker exec ai_context_postgres psql -U ai_context -d ai_context \
    -tAc "SELECT 1 FROM projects WHERE project_key = '${PROJECT_KEY}';" 2>/dev/null || echo "")
  if [[ -z "$REGISTERED" ]]; then
    # ── Check for available backup ────────────────────────────────────────────
    BACKUP_META="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/backup_meta.json"
    BACKUP_PG="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/postgres.json"
    BACKUP_SNAP="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/qdrant.snapshot"
    BACKUP_JSON="$(jq -r '.backup_dir' "$RESOLVED")/${PROJECT_KEY}/qdrant.vectors.json"

    # A restorable backup requires postgres.json + at least one Qdrant file.
    # backup_meta.json is OPTIONAL — a manually provided backup may not have it.
    QDRANT_FILE_EXISTS=false
    [[ -f "$BACKUP_SNAP" ]] && QDRANT_FILE_EXISTS=true
    [[ -f "$BACKUP_JSON" ]] && QDRANT_FILE_EXISTS=true

    # ── Check for a pre-selected restore choice (written by clean.sh in reset flow) ─
    RESTORE_CHOICE_FILE="$AI_CONTEXT_DIR/.restore-choice-${PROJECT_KEY}"
    if [[ -f "$RESTORE_CHOICE_FILE" ]]; then
      PRESELECTED=$(cat "$RESTORE_CHOICE_FILE")
      rm -f "$RESTORE_CHOICE_FILE"
      if [[ "$PRESELECTED" == "restore" ]]; then
        echo "[ai-context-up] Restoring '$PROJECT_KEY' from backup (chosen during reset)..."
        bash "$AI_CONTEXT_DIR/scripts/restore.sh" "$PROJECT_KEY"
        ui_line_reset
      else
        echo "[ai-context-up] Running full init for '$PROJECT_KEY' (chosen during reset)..."
        bash "$AI_CONTEXT_DIR/scripts/init.sh" "$PROJECT_KEY"
        ui_line_reset
      fi
    elif [[ "$QDRANT_FILE_EXISTS" == "true" ]] && [[ -f "$BACKUP_PG" ]]; then
      echo ""
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
          echo "           JSON    : $(date -r "$BACKUP_JSON" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 'unknown') (file mtime)"
        fi
        if [[ -f "$BACKUP_SNAP" ]]; then
          echo "           Snapshot: $(date -r "$BACKUP_SNAP" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 'unknown') (file mtime)"
        fi
      fi
      echo "           (newer file will be used for restore)"
      echo ""
      # Only prompt if stdin is a terminal (non-interactive CI environments skip restore).
      if [ -t 0 ]; then
        read -r -p "  Restore from backup instead of full re-index? [Y/n] " _RESTORE_CHOICE
      else
        _RESTORE_CHOICE="n"
        echo "  (non-interactive mode — skipping restore, running full init)"
      fi
      case "${_RESTORE_CHOICE:-Y}" in
        [Yy]*)
          echo "[ai-context-up] Restoring '$PROJECT_KEY' from backup..."
          bash "$AI_CONTEXT_DIR/scripts/restore.sh" "$PROJECT_KEY"
          ui_line_reset
          ;;
        *)
          echo "[ai-context-up] Running full init for '$PROJECT_KEY'..."
          bash "$AI_CONTEXT_DIR/scripts/init.sh" "$PROJECT_KEY"
          ui_line_reset
          ;;
      esac
    else
      echo "[ai-context-up] First run for '$PROJECT_KEY' — running init..."
      bash "$AI_CONTEXT_DIR/scripts/init.sh" "$PROJECT_KEY"
      ui_line_reset
    fi
  else
    ui_line "[ai-context-up]" "'$PROJECT_KEY' already initialised — skipping init."
  fi
done < <(jq -r '.projects | keys[]' "$RESOLVED")

# ── Step 6: Start consolidated containers once for all projects ────────────────
echo "[ai-context-up] Starting consolidated AI context containers..."
bash "$AI_CONTEXT_DIR/scripts/start.sh"

# ── Register MCP servers in IDE configs (idempotent) ─────────────────────────
bash "$AI_CONTEXT_DIR/scripts/init_ide.sh"

# ── Step 6: Print summary ─────────────────────────────────────────────────────
REPO_SLUG=$(basename "$ROOT_DIR")
MCP_CONTAINER="${REPO_SLUG}_context_mcp"
WATCHER_CONTAINER="${REPO_SLUG}_context_watcher"

# MCP server URLs are already shown live in start.sh's dashboard (on the
# "start mcp server" row) — no need to repeat them here in a second table.
SEP="──────────────────────────────────────────────────────────────────────"
echo ""
echo "$SEP"
echo ""
echo "  Logs:  docker logs -f $WATCHER_CONTAINER   |   docker logs -f $MCP_CONTAINER"
echo ""

if [[ "$DAEMON" == "true" ]]; then
  # Detach: disable the cleanup trap so returning control to the shell (a
  # normal exit) doesn't stop what was just started. Docker itself already
  # runs these containers detached regardless of this flag — this only
  # controls whether the SCRIPT stays attached to catch Ctrl-C.
  trap - EXIT INT TERM
  echo "  Running in daemon mode -- returning control to your shell."
  echo "  Stop later with:  mise run ai-context-down"
  echo ""
  exit 0
fi

echo "  Press Ctrl-C to stop all services."
echo ""

# ── Keep alive ────────────────────────────────────────────────────────────────
while true; do sleep 5; done
