#!/usr/bin/env bash
# init.sh PROJECT_KEY — First-time setup for one project.
# Called by ai-context-up.sh for each project that needs initialisation.
# Reads project config from .resolved.json (written by validate_config.py).
set -euo pipefail

PROJECT_KEY="${1:?Usage: init.sh <project-key>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CONTEXT_DIR="$(dirname "$SCRIPT_DIR")"
RESOLVED="$AI_CONTEXT_DIR/.resolved.json"

source "$SCRIPT_DIR/lib/build_helpers.sh"
require_cmd jq     "brew install jq"
require_cmd docker "Install Rancher Desktop from rancherdesktop.io"

if [ ! -f "$RESOLVED" ]; then
  echo "ERR .resolved.json not found. Run: mise run ai-context-up  (validates and writes it)."
  exit 1
fi

DISPLAY=$(jq    -r ".projects[\"$PROJECT_KEY\"].display_name"        "$RESOLVED")
MCP_PORT=$(jq   -r ".projects[\"$PROJECT_KEY\"].mcp.port"            "$RESOLVED")
ROOT_PATH=$(jq  -r ".projects[\"$PROJECT_KEY\"].root_path"           "$RESOLVED")
# Vector dimension is hardcoded, not read from config — it's fixed by the
# shared inference service's hardcoded embedding model (nomic-embed-text-v1.5,
# see inference/main.py's EMBEDDING_DIMENSION), not a per-project config.yaml
# field anymore.
DIMENSION=768
PROFILE=$(jq    -c ".projects[\"$PROJECT_KEY\"].profile"             "$RESOLVED")
REPO_ROOT=$(jq  -r '.repo_root'                                      "$RESOLVED")

if [ -n "$ROOT_PATH" ] && [ "$ROOT_PATH" != "null" ]; then
  PROJECT_ROOT_ABS="$REPO_ROOT/$ROOT_PATH"
else
  PROJECT_ROOT_ABS="$REPO_ROOT"
fi

echo ""
echo "==> Initialising: $DISPLAY ($PROJECT_KEY)"
echo "    Root: $PROJECT_ROOT_ABS"

# ── Guard: shared DB must be running ──────────────────────────────────────────
if ! docker ps --format "{{.Names}}" | grep -q "^ai_context_qdrant$"; then
  echo "ERR Shared DB stack not running. Run: mise run ai-context-infra-up"
  exit 1
fi

# ── Guard: required models must be available before overwriting any DB state ────
ui_task "Checking configured models" -- python3 "$AI_CONTEXT_DIR/scripts/check_models.py" --resolved "$RESOLVED"

# ── Register project in Postgres ───────────────────────────────────────────────
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

# ── Create Qdrant collection ───────────────────────────────────────────────────
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

# ── Build Docker images (shared — only needs to happen once across all projects) ─
if ! docker image inspect ai-context-watcher:latest >/dev/null 2>&1 || \
   ! docker image inspect ai-context-mcp:latest     >/dev/null 2>&1; then
  echo "--> Building Docker images..."
  build_image ai-context-watcher:latest "$AI_CONTEXT_DIR/watcher/"
  build_image ai-context-mcp:latest     "$AI_CONTEXT_DIR/mcp-server/"
fi

# ── Full initial index ─────────────────────────────────────────────────────────
# The indexer logs one line per file ("INDEXED: path (chunks: N, ...)"), which
# floods the terminal for a multi-minute run on a large repo. ui_task shows a
# live tail of that real progress while it runs, then collapses the whole
# block away on success — only a failure keeps the full log on screen.
ui_task "Running full initial index (this may take a few minutes)" -- \
  docker run --rm \
  --name "ai_context_index_${PROJECT_KEY}" \
  --label "com.docker.compose.project=ai-context" \
  --network ai-context-net \
  -v "$PROJECT_ROOT_ABS:/workspace:ro" \
  -v "$RESOLVED:/app/.resolved.json:ro" \
  -e MODE=full \
  -e PROJECT_KEY="$PROJECT_KEY" \
  -e PROJECT_ROOT=/workspace \
  -e QDRANT_URL=http://ai_context_qdrant:6333 \
  -e POSTGRES_URL=postgresql://ai_context:ai_context_local@ai_context_postgres:5432/ai_context \
  -e OMLX_API_KEY="${OMLX_API_KEY:-}" \
  --entrypoint python \
  ai-context-watcher:latest \
  entrypoint.py

# ── Generate L0 project map ────────────────────────────────────────────────────
ui_task "Generating L0 project map" -- \
  docker run --rm \
  --name "ai_context_l0_${PROJECT_KEY}" \
  --label "com.docker.compose.project=ai-context" \
  --network ai-context-net \
  -v "$PROJECT_ROOT_ABS:/workspace:ro" \
  -v "$RESOLVED:/app/.resolved.json:ro" \
  -e MODE=l0 \
  -e PROJECT_KEY="$PROJECT_KEY" \
  -e PROJECT_ROOT=/workspace \
  -e QDRANT_URL=http://ai_context_qdrant:6333 \
  -e POSTGRES_URL=postgresql://ai_context:ai_context_local@ai_context_postgres:5432/ai_context \
  -e OMLX_API_KEY="${OMLX_API_KEY:-}" \
  --entrypoint python \
  ai-context-watcher:latest \
  entrypoint.py

echo "==> Done: $PROJECT_KEY"
