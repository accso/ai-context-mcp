#!/usr/bin/env bash
# restore.sh PROJECT_KEY — Restore one project from its database backup.
#
# Called by ai-context-up.sh when the project is not yet registered and the user
# accepts restoring from a previous backup instead of running a full re-index.
#
# Steps:
#   1. Register project in PostgreSQL
#   2. Create Qdrant collection (must exist before snapshot upload)
#   3. Build Docker images if not already present
#   4. Run watcher container in MODE=restore (uploads snapshot + imports Postgres data)
#
# After this script the project is registered with the restored state.
# The watcher will then start normally via start.sh, picking up any fs changes since
# the backup was created through its standard delta-index mechanism.
set -euo pipefail

PROJECT_KEY="${1:?Usage: restore.sh <project-key>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CONTEXT_DIR="$(dirname "$SCRIPT_DIR")"
RESOLVED="$AI_CONTEXT_DIR/.resolved.json"
BACKUP_DIR="$(jq -r '.backup_dir' "$RESOLVED")/$PROJECT_KEY"

source "$SCRIPT_DIR/lib/build_helpers.sh"
require_cmd jq     "brew install jq"
require_cmd docker "Install Rancher Desktop from rancherdesktop.io"

if [ ! -f "$RESOLVED" ]; then
  echo "ERR .resolved.json not found. Run: mise run ai-context-up  (validates and writes it)."
  exit 1
fi

# Determine Qdrant backup format (snapshot takes precedence over json vectors)
QDRANT_FORMAT=""
if   [ -f "$BACKUP_DIR/qdrant.snapshot"      ]; then QDRANT_FORMAT="snapshot"
elif [ -f "$BACKUP_DIR/qdrant.vectors.json"  ]; then QDRANT_FORMAT="json"
fi

if [ -z "$QDRANT_FORMAT" ] || [ ! -f "$BACKUP_DIR/postgres.json" ]; then
  echo "ERR Incomplete or missing backup at $BACKUP_DIR"
  echo "    Expected: postgres.json  +  qdrant.snapshot  OR  qdrant.vectors.json"
  exit 1
fi

DISPLAY=$(jq    -r ".projects[\"$PROJECT_KEY\"].display_name"        "$RESOLVED")
MCP_PORT=$(jq   -r ".projects[\"$PROJECT_KEY\"].mcp.port"            "$RESOLVED")
# Vector dimension is hardcoded, not read from config — it's fixed by the
# shared inference service's hardcoded embedding model (nomic-embed-text-v1.5,
# see inference/main.py's EMBEDDING_DIMENSION), not a per-project config.yaml
# field anymore.
DIMENSION=768
PROFILE=$(jq    -c ".projects[\"$PROJECT_KEY\"].profile"             "$RESOLVED")
ROOT_PATH=$(jq  -r ".projects[\"$PROJECT_KEY\"].root_path"           "$RESOLVED")
REPO_ROOT=$(jq  -r '.repo_root'                                      "$RESOLVED")

echo ""
echo "==> Restoring: $DISPLAY ($PROJECT_KEY)"
echo "    Root: ${REPO_ROOT:+$REPO_ROOT/}${ROOT_PATH}"

# ── Guard: shared DB must be running ──────────────────────────────────────────
if ! docker ps --format "{{.Names}}" | grep -q "^ai_context_qdrant$"; then
  echo "ERR Shared DB stack not running. Run: mise run ai-context-infra-up"
  exit 1
fi

# ── Step 1: Register project in Postgres ──────────────────────────────────────
echo "--> Registering project in Postgres..."
_sq() { printf '%s' "$1" | sed "s/'/''/g"; }
docker exec ai_context_postgres psql -U ai_context -d ai_context -c \
  "INSERT INTO projects (project_key, display_name, profile, root_path)
   VALUES ('$(_sq "$PROJECT_KEY")', '$(_sq "$DISPLAY")', '$(_sq "$PROFILE")', '$(_sq "$ROOT_PATH")')
   ON CONFLICT (project_key) DO UPDATE SET
     display_name = EXCLUDED.display_name,
     profile      = EXCLUDED.profile,
     root_path    = EXCLUDED.root_path;" > /dev/null
echo "    OK project registered."

# ── Step 2: Create Qdrant collection ─────────────────────────────────────────
# The collection must exist before the snapshot upload can target it.
echo "--> Creating Qdrant collection: $PROJECT_KEY ..."
HTTP_STATUS=$(docker run --rm --network ai-context-net curlimages/curl:latest \
  -s -o /dev/null -w "%{http_code}" -X PUT \
  "http://ai_context_qdrant:6333/collections/${PROJECT_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"vectors\":{\"size\":$DIMENSION,\"distance\":\"Cosine\"},\"optimizers_config\":{\"default_segment_number\":2}}")
if [ "$HTTP_STATUS" = "200" ] || [ "$HTTP_STATUS" = "409" ]; then
  echo "    OK Qdrant collection ready."
else
  echo "ERR Qdrant HTTP $HTTP_STATUS"; exit 1
fi

# ── Step 3: Build Docker images if missing ────────────────────────────────────
if ! docker image inspect ai-context-watcher:latest >/dev/null 2>&1 || \
   ! docker image inspect ai-context-mcp:latest     >/dev/null 2>&1; then
  echo "--> Building Docker images..."
  build_image ai-context-watcher:latest "$AI_CONTEXT_DIR/watcher/"
  build_image ai-context-mcp:latest     "$AI_CONTEXT_DIR/mcp-server/"
fi

# ── Step 4: Restore from backup ───────────────────────────────────────────────
# Mount the backup directory read-only; the restore container uploads the
# Qdrant snapshot and imports the Postgres data, then exits. The restore
# process logs real progress (upload %, batch upsert counts) — shown live via
# ui_task's tail pane, then collapsed away on success.
ui_task "Restoring from backup (format: $QDRANT_FORMAT)" -- \
  docker run --rm \
  --name "ai_context_restore_${PROJECT_KEY}" \
  --label "com.docker.compose.project=ai-context" \
  --network ai-context-net \
  -v "${BACKUP_DIR}:/backup:ro" \
  -v "${RESOLVED}:/app/.resolved.json:ro" \
  -e MODE=restore \
  -e PROJECT_KEY="$PROJECT_KEY" \
  -e QDRANT_FORMAT="$QDRANT_FORMAT" \
  -e QDRANT_URL=http://ai_context_qdrant:6333 \
  -e POSTGRES_URL=postgresql://ai_context:ai_context_local@ai_context_postgres:5432/ai_context \
  --entrypoint python \
  ai-context-watcher:latest \
  entrypoint.py

echo ""
echo "==> Restored: $PROJECT_KEY"
echo "    The watcher will pick up any file changes made since the backup was created."
echo "    If many files changed, consider: mise run ai-context-reset (full re-index)."

