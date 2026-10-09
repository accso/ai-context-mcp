#!/usr/bin/env bash
# search.sh — Query a running MCP server's context index from the terminal.
#
# Usage:
#   search.sh "<query>"
#   search.sh "<query>" <project-key>
#
# Examples:
#   search.sh "user authentication and JWT"
#   search.sh "PlatformUserRepository" my-platform-backend
#   search.sh "Koin module dependency injection" my-platform-components
#
# The /search REST endpoint is used directly — no MCP JSON-RPC handshake
# required.  The output is identical to what the search_context MCP tool
# returns (Markdown-formatted L0 + L1 + L2 context).
#
# Requires: jq, curl
# The MCP server for the target project must be running:
#   mise run ai-context-up
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CONTEXT_DIR="$(dirname "$SCRIPT_DIR")"
RESOLVED="$AI_CONTEXT_DIR/.resolved.json"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERR '$1' not found. Install: $2"; exit 1; }
}
require_cmd jq   "brew install jq"
require_cmd curl "brew install curl"

# ── Argument parsing ──────────────────────────────────────────────────────────

if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
  echo "Usage: $0 \"<query>\" [project-key]"
  echo ""
  echo "Available projects (from .resolved.json):"
  if [ -f "$RESOLVED" ]; then
    jq -r '.projects | to_entries[] | "  \(.key)  (port \(.value.mcp.port))"' "$RESOLVED"
  else
    echo "  (run 'mise run ai-context-up' first to generate .resolved.json)"
  fi
  exit 1
fi

QUERY="$1"
PROJECT_KEY="${2:-}"

# ── Resolve config ────────────────────────────────────────────────────────────

if [ ! -f "$RESOLVED" ]; then
  echo "ERR .resolved.json not found. Run: mise run ai-context-up"
  exit 1
fi

if [ -z "$PROJECT_KEY" ]; then
  PROJECT_KEY=$(jq -r '.projects | keys | .[0]' "$RESOLVED")
  echo "No project specified — using first: $PROJECT_KEY"
fi

PORT=$(jq -r ".projects[\"$PROJECT_KEY\"].mcp.port // empty" "$RESOLVED")
if [ -z "$PORT" ]; then
  echo "ERR Project '$PROJECT_KEY' not found in .resolved.json."
  echo "    Available:"
  jq -r '.projects | keys[] | "  " + .' "$RESOLVED"
  exit 1
fi

URL="http://localhost:${PORT}/search"

# ── Execute ───────────────────────────────────────────────────────────────────

echo "── search_context ─────────────────────────────────────────────────────"
printf "  project : %s  (port %s)\n" "$PROJECT_KEY" "$PORT"
printf "  query   : %s\n" "$QUERY"
echo "───────────────────────────────────────────────────────────────────────"
echo ""

# POST is used so queries with special chars don't need URL-encoding.
RESPONSE_HEADERS=$(mktemp)
RESPONSE_BODY=$(
  curl -sf -X POST "$URL" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg q "$QUERY" '{"query":$q}')" \
    --max-time 120 \
    -D "$RESPONSE_HEADERS" \
    2>&1
) || {
  CODE=$?
  echo "ERR Failed to reach $URL (exit $CODE)."
  echo "    Is the MCP server running?  Start with: mise run ai-context-up"
  rm -f "$RESPONSE_HEADERS"
  exit 1
}

# Print response headers as footer metadata
CHUNKS=$(grep -i "^x-chunk-count:"      "$RESPONSE_HEADERS" 2>/dev/null | tr -d '\r' | awk '{print $2}' || echo "?")
TOKENS=$(grep -i "^x-estimated-tokens:" "$RESPONSE_HEADERS" 2>/dev/null | tr -d '\r' | awk '{print $2}' || echo "?")
SOURCES=$(grep -i "^x-sources:"         "$RESPONSE_HEADERS" 2>/dev/null | tr -d '\r' | sed 's/x-sources: //I' || echo "?")
rm -f "$RESPONSE_HEADERS"

echo "$RESPONSE_BODY"
echo ""
echo "───────────────────────────────────────────────────────────────────────"
printf "  chunks: %s   tokens: ~%s   sources: %s\n" "$CHUNKS" "$TOKENS" "$SOURCES"
echo "───────────────────────────────────────────────────────────────────────"

