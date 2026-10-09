#!/usr/bin/env bash
# test-cleanup.sh — idempotent teardown/recovery for .ai-context test-created
# Docker resources (Tier 2/3: disposable Postgres/Qdrant, the E2E compose
# stack). Safe to run any time, safe to re-run, safe to run when there's
# nothing to clean up.
#
# This is the actual safety net against "test docker containers left running
# silently in the background": scripts/test.sh traps EXIT/INT/TERM around
# every integration/e2e run to call this automatically, but an agent's own
# shell session can itself be killed before a trap fires — so this script
# must also be independently callable at any time to recover from that case.
# It is additionally run as a pre-flight step before every integration/e2e
# run, so a previous crashed run's leftovers never collide with a new one.
#
# Everything this script can remove is scoped to two markers, applied to
# every test-created Docker resource (containers, networks, volumes):
#   - label:   com.ai-context.test=true
#   - compose project name: ai-context-test
# Neither marker is ever used by the real dev stack (compose projects
# "ai-context" / "ai-context-infrastructure", see infrastructure/docker-compose.yml
# and scripts/start.sh) — so this script can never remove a developer's real,
# already-running containers, only what tests themselves created.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CONTEXT_DIR="$(dirname "$SCRIPT_DIR")"
TEST_LABEL="com.ai-context.test=true"
TEST_COMPOSE_PROJECT="ai-context-test"
TEST_COMPOSE_FILE="$AI_CONTEXT_DIR/tests/e2e/docker-compose.test.yml"

usage() {
  cat <<'EOF'
Usage: test-cleanup.sh [--check]

  (no args)   Remove all Docker containers/networks/volumes created by
              .ai-context test runs (label=com.ai-context.test=true or
              compose project=ai-context-test). Idempotent — safe to
              re-run, safe when there's nothing to clean up.
  --check     List lingering test resources without removing them.
              Exits non-zero if anything is found, 0 if already clean —
              suitable for scripting/agent use.
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --check)   CHECK_ONLY=true ;;
  "")        CHECK_ONLY=false ;;
  *)         echo "ERR unknown argument: $1" >&2; usage >&2; exit 2 ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "==> docker not found on PATH — nothing to clean up (Tier 2/3 never ran)."
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "==> Docker daemon not reachable — nothing to clean up (or already stopped)."
  exit 0
fi

echo "==> Scanning for .ai-context test resources (label=$TEST_LABEL, compose project=$TEST_COMPOSE_PROJECT)..."

by_label_containers=$(docker ps -aq --filter "label=$TEST_LABEL" 2>/dev/null || true)
by_compose_containers=$(docker ps -aq --filter "label=com.docker.compose.project=$TEST_COMPOSE_PROJECT" 2>/dev/null || true)
containers=$(printf '%s\n%s\n' "$by_label_containers" "$by_compose_containers" | sort -u | grep -v '^$' || true)
networks=$(docker network ls -q --filter "label=$TEST_LABEL" 2>/dev/null || true)
volumes=$(docker volume ls -q --filter "label=$TEST_LABEL" 2>/dev/null || true)

if [[ -z "$containers" && -z "$networks" && -z "$volumes" ]]; then
  echo "==> Nothing to clean up. Machine is clean."
  exit 0
fi

echo "Found lingering test resources:"
[[ -n "$containers" ]] && echo "  Containers: $(echo "$containers" | tr '\n' ' ')"
[[ -n "$networks"   ]] && echo "  Networks:   $(echo "$networks" | tr '\n' ' ')"
[[ -n "$volumes"    ]] && echo "  Volumes:    $(echo "$volumes" | tr '\n' ' ')"

if [[ "${CHECK_ONLY:-false}" == "true" ]]; then
  echo "==> --check mode: not removing anything."
  exit 1
fi

echo "==> Removing..."

# Prefer compose-native teardown first — handles the E2E stack's own network
# cleanly. Safe no-op if the compose file doesn't exist yet (Tier 3 not
# implemented yet) or the project was already torn down.
if [[ -f "$TEST_COMPOSE_FILE" ]]; then
  docker compose -p "$TEST_COMPOSE_PROJECT" -f "$TEST_COMPOSE_FILE" down --volumes --remove-orphans 2>/dev/null || true
fi

# Catch-all for anything compose didn't own (ad-hoc `docker run` disposable
# instances from Tier 2 suites).
[[ -n "$containers" ]] && docker rm -f $containers >/dev/null 2>&1 || true
[[ -n "$networks"   ]] && docker network rm $networks >/dev/null 2>&1 || true
[[ -n "$volumes"    ]] && docker volume rm $volumes >/dev/null 2>&1 || true

echo "==> Cleanup complete."
