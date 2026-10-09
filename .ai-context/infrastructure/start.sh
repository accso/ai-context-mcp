#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start.sh — Start the shared AI Context services (qdrant + postgres +
# inference). Part of the "ai-context-infrastructure" compose project — fully
# independent of the per-project "ai-context" MCP server / watcher containers.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Ensure external network and volumes exist before compose runs ─────────────
echo "==> Ensuring shared network and volumes exist..."
docker network create ai-context-net           2>/dev/null && echo "    Created network:  ai-context-net"           || echo "    Network exists:  ai-context-net"
docker volume   create ai_context_qdrant_data  2>/dev/null && echo "    Created volume:   ai_context_qdrant_data"   || echo "    Volume exists:   ai_context_qdrant_data"
docker volume   create ai_context_postgres_data 2>/dev/null && echo "    Created volume:   ai_context_postgres_data" || echo "    Volume exists:   ai_context_postgres_data"

echo ""
echo "==> Starting AI Context shared services (qdrant, postgres, inference)..."
cd "$SCRIPT_DIR"
docker compose up -d --build qdrant postgres inference

echo ""
echo "   Waiting for services to be healthy..."
for service in ai_context_qdrant ai_context_postgres ai_context_inference; do
  echo -n "   Waiting for $service..."
  until [ "$(docker inspect -f '{{.State.Health.Status}}' "$service" 2>/dev/null)" = "healthy" ]; do
    sleep 2
    echo -n "."
  done
  echo " ready"
done

echo ""
echo "   Shared services are ready."
echo "   Qdrant:    http://localhost:6333"
echo "   Postgres:  localhost:5433  (db: ai_context, user: ai_context)"
echo "   Inference: (internal-only — embeddings + re-ranking for all projects)"
echo ""

# ── Apply schema migrations (idempotent — all statements use IF NOT EXISTS) ──
# Runs on every start so new tables/indexes added to init.sql are never
# silently absent on an existing volume.
echo "==> Applying DB schema (idempotent)..."
docker exec -i ai_context_postgres psql -U ai_context -d ai_context \
  < "$SCRIPT_DIR/init.sql" | grep -v "^NOTICE" || true
echo "    Schema up-to-date."
echo ""

