#!/usr/bin/env bash
# test.sh {unit|integration|e2e|all} [component] — single, tier-selectable
# entrypoint for .ai-context's test suite, so an agent (or human) has one
# predictable command regardless of which file it changed, and can scope to
# just the tier/component matching the blast radius of its change:
#
#   scripts/test.sh unit                 # Tier 1, every component (seconds)
#   scripts/test.sh unit watcher         # Tier 1, just watcher/
#   scripts/test.sh integration          # Tier 1+2, every component (needs Docker)
#   scripts/test.sh e2e                  # Tier 1+2+3, full stack (needs Docker, minutes)
#   scripts/test.sh all                  # every tier, every component
#
# Components: watcher, lsp-bridge, scripts, inference (Python, pytest) and mcp-server
# (TypeScript, node --test). Excluded: .ai-monitoring, .ai-benchmark
# (separate tools, moving to their own repos — see
# docs/next-plans/consolidated-backlog-and-serena-roadmap.md).
#
# Cleanup: integration/e2e runs are wrapped in a trap so
# scripts/test-cleanup.sh always fires on exit — pass, fail, or interrupt
# (Ctrl-C) — and also run once as a pre-flight step first, so a previous
# crashed run's leftovers never collide with this one.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CONTEXT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$AI_CONTEXT_DIR"

PY_COMPONENTS=(watcher lsp-bridge scripts inference)
TS_COMPONENTS=(mcp-server)

usage() {
  cat <<'EOF'
Usage: test.sh {unit|integration|e2e|all} [component]

  unit          Tier 1 — pure functions, no Docker/network. Seconds.
  integration   Tier 1+2 — adds disposable Postgres/Qdrant. Tens of seconds.
  e2e           Tier 1+2+3 — adds the full docker-compose stack. Minutes.
  all           Every tier.

  component     Optional: one of watcher, lsp-bridge, scripts, inference, mcp-server.
                Omit to run every applicable component for the chosen tier.
EOF
}

TIER="${1:-}"
COMPONENT="${2:-}"

case "$TIER" in
  unit|integration|e2e|all) ;;
  -h|--help) usage; exit 0 ;;
  *) echo "ERR unknown tier: '${TIER:-<none>}'" >&2; usage >&2; exit 2 ;;
esac

if [[ -n "$COMPONENT" ]]; then
  case "$COMPONENT" in
    watcher|lsp-bridge|scripts|inference|mcp-server) ;;
    *) echo "ERR unknown component: $COMPONENT (expected one of: watcher, lsp-bridge, scripts, inference, mcp-server)" >&2; exit 2 ;;
  esac
fi

FAILED=0

run_py_tier() {
  local py_tier="$1"  # unit|integration
  for c in "${PY_COMPONENTS[@]}"; do
    if [[ -n "$COMPONENT" && "$COMPONENT" != "$c" ]]; then continue; fi
    local dir="$c/tests/$py_tier"
    if [[ ! -d "$dir" ]]; then continue; fi
    # Skip (don't fail) a tier directory that exists but has no test files
    # yet — pytest exits non-zero ("no tests collected") for a genuinely
    # empty directory, which is a legitimate not-yet-written state for a
    # given component/tier, not a test failure.
    if [[ -z "$(find "$dir" -name 'test_*.py' -print -quit)" ]]; then continue; fi
    echo "==> pytest $dir"
    if ! python3 -m pytest "$dir"; then FAILED=1; fi
  done
}

run_ts_tier() {
  local ts_tier="$1"  # unit|integration|e2e
  for c in "${TS_COMPONENTS[@]}"; do
    if [[ -n "$COMPONENT" && "$COMPONENT" != "$c" ]]; then continue; fi
    echo "==> npm run test:$ts_tier --prefix $c"
    if ! npm run "test:$ts_tier" --prefix "$c"; then FAILED=1; fi
  done
}

run_unit() {
  echo "### Tier 1 — unit ###"
  run_py_tier unit
  run_ts_tier unit
}

run_integration() {
  echo "### Tier 2 — integration (Docker required) ###"
  echo "--> Pre-flight cleanup (clearing any leftovers from a previous crashed run)..."
  "$SCRIPT_DIR/test-cleanup.sh" || true
  trap '"$SCRIPT_DIR/test-cleanup.sh" || true' EXIT INT TERM
  run_py_tier integration
  run_ts_tier integration
}

run_e2e() {
  echo "### Tier 3 — e2e (full docker-compose stack required) ###"
  echo "--> Pre-flight cleanup (clearing any leftovers from a previous crashed run)..."
  "$SCRIPT_DIR/test-cleanup.sh" || true
  trap '"$SCRIPT_DIR/test-cleanup.sh" || true' EXIT INT TERM
  run_ts_tier e2e
}

case "$TIER" in
  unit)        run_unit ;;
  integration) run_unit; run_integration ;;
  e2e)         run_unit; run_integration; run_e2e ;;
  all)         run_unit; run_integration; run_e2e ;;
esac

if [[ "$FAILED" -ne 0 ]]; then
  echo "==> FAILED — see output above."
  exit 1
fi
echo "==> All requested tests passed."
