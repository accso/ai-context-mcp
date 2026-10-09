#!/usr/bin/env bash
# start.sh — Start consolidated AI context MCP server, watcher, and LSP bridge
#            containers (one of each per repo — see
#            docs/lsp-integration.md).
#
# Usage:
#   start.sh              — start the consolidated containers for all projects
#   start.sh <project>    — no-op for compatibility with legacy per-project callers
#
# The inference service (embeddings + re-ranking) is NOT managed here — it is
# shared infra, independent of any project, started/stopped via
# `mise run ai-context-infra-up`. This script only detects whether it's
# already running, to wire INFERENCE_URL into the MCP server and watcher.
#
# Images are always rebuilt from source so any source changes are picked up
# automatically — no manual `docker build` needed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CONTEXT_DIR="$(dirname "$SCRIPT_DIR")"
RESOLVED="$AI_CONTEXT_DIR/.resolved.json"
REPO_SLUG=$(basename "$(dirname "$AI_CONTEXT_DIR")")
MCP_CONTAINER="${REPO_SLUG}_context_mcp"
WATCHER_CONTAINER="${REPO_SLUG}_context_watcher"
LSP_CONTAINER="${REPO_SLUG}_context_lsp"

source "$SCRIPT_DIR/lib/build_helpers.sh"
require_cmd jq     "brew install jq"
require_cmd docker "Install Rancher Desktop from rancherdesktop.io"

if [ ! -f "$RESOLVED" ]; then
  echo "ERR .resolved.json not found. Run: mise run ai-context-up  (validates and writes it)."
  exit 1
fi

# ── Compute which LSP language toolchains are actually needed ─────────────────
# Union of every registered project's auto-detected lsp.enabled_languages,
# so the lsp-bridge image only installs toolchains at least one project
# actually needs — see docs/lsp-integration.md
# conditional-install follow-up (avoids re-downloading toolchains like the
# ~3.3GB Swift toolchain on every cache-invalidating rebuild when nothing
# uses them).
LSP_LANGUAGES=$(jq -r '[.projects[].lsp.enabled_languages[]?] | unique | join(",")' "$RESOLVED")
ui_line "[start]" "LSP languages needed by registered projects: ${LSP_LANGUAGES:-<none>}"

# ── Dependency cache volumes (see docs/lsp-integration.md) ──
# One shared, persistent volume per ecosystem, reused across every registered
# project — same rationale as the shared ai_context_qdrant_data/postgres_data
# volumes in infrastructure/start.sh. Only created for languages at least one
# registered project actually needs (mirrors the LSP_LANGUAGES-gated image
# build above). `docker volume create` is idempotent and near-instant, so
# this runs silently — no per-volume line, consistent with this repo's
# noise-reduction work on these scripts.
declare -A DEP_CACHE_VOLUME_FOR=(
  [go]=ai_context_go_mod_cache
  [rust]=ai_context_cargo_registry
  [csharp]=ai_context_nuget_packages
  [ruby]=ai_context_gem_cache
  [dart]=ai_context_pub_cache
)
for _lang in "${!DEP_CACHE_VOLUME_FOR[@]}"; do
  case ",$LSP_LANGUAGES," in
    *",$_lang,"*) docker volume create "${DEP_CACHE_VOLUME_FOR[$_lang]}" >/dev/null 2>&1 || true ;;
  esac
done
unset _lang
# Python: unlike every other ecosystem above, a flat shared site-packages
# directory is NOT safe to reuse across projects — pip installs aren't
# version-keyed on disk the way Go modules/crates/NuGet packages/gems/Maven
# artifacts are (two projects needing different versions of the same
# package would collide). Only the wheel *download* cache is safe to share
# flatly (wheel filenames are version-keyed); the restore step installs
# each project's resolved packages into a project_key-namespaced
# subdirectory of this same volume instead of a shared site-packages root
# — see the Python row in docs/lsp-integration.md's dependency-resolution table.
case ",$LSP_LANGUAGES," in
  *,python,*) docker volume create ai_context_pip_cache >/dev/null 2>&1 || true ;;
esac
# Swift: SPM resolves into `.build/checkouts` INSIDE the package directory
# by default — same read-only-mount conflict as Dart, except SPM has a
# working redirect flag (`swift package resolve --scratch-path`), so this
# is wired properly rather than left degraded. Namespaced per-project like
# Python, since a scratch path holds one package's full resolved graph.
case ",$LSP_LANGUAGES," in
  *,swift,*) docker volume create ai_context_swiftpm_cache >/dev/null 2>&1 || true ;;
esac
# Java and Kotlin both can use either Maven or Gradle (a Java project may
# well use Gradle, and vice versa) — create both cache volumes whenever
# either language is enabled, rather than trying to guess the build tool.
case ",$LSP_LANGUAGES," in
  *,java,*|*,kotlin,*)
    docker volume create ai_context_maven_repository >/dev/null 2>&1 || true
    docker volume create ai_context_gradle_cache      >/dev/null 2>&1 || true
    ;;
esac

REPO_ROOT=$(jq -r '.repo_root' "$RESOLVED")

# ── Read shared config flags ──────────────────────────────────────────────────
RERANKER_TOP_N=$(jq -r '.reranker.top_n // 20' "$RESOLVED")

# ── Wire up the shared inference service (embeddings + re-ranking), if already
#    running. Mandatory infra (see infrastructure/docker-compose.yml) — unlike
#    the reranker-only service this replaces, there is no "disabled" state to
#    check for, only "not started yet".
if docker ps --format "{{.Names}}" | grep -q "^ai_context_inference$"; then
  INFERENCE_URL="http://ai_context_inference:8080"
else
  echo "[start] NOTE: the shared inference service isn't running (needed for embeddings + re-ranking)."
  echo "        Start it with: mise run ai-context-infra-up"
  ui_line_reset  # this NOTE wasn't printed via ui_line — don't let the next
                 # "[start]" ui_line call overwrite it.
  INFERENCE_URL=""
fi

# ── Build port list and volume mounts from config ─────────────────────────────
PORT_ARGS=()
GITIGNORE_MOUNTS=()

while IFS= read -r PROJECT_KEY; do
  MCP_PORT=$(jq -r ".projects[\"$PROJECT_KEY\"].mcp.port" "$RESOLVED")

  # MCP: expose each project's port directly on the host.
  PORT_ARGS+=("-p" "${MCP_PORT}:${MCP_PORT}")

  # Extra gitignore files (monorepo top-level .gitignore etc.) must still be
  # bind-mounted at fixed, ordered paths. Each project gets its own namespace
  # under /extra-gitignores/{project_key}/ so two projects' extra gitignores
  # cannot collide inside the single consolidated container.
  i=0
  while IFS= read -r extra_path; do
    [ -n "$extra_path" ] || continue
    GITIGNORE_MOUNTS+=("-v" "${extra_path}:/extra-gitignores/${PROJECT_KEY}/${i}.gitignore:ro")
    i=$((i + 1))
  done < <(jq -r ".projects[\"$PROJECT_KEY\"].indexer.extra_gitignore_paths[]?" "$RESOLVED")
done < <(jq -r '.projects | keys[]' "$RESOLVED")

# Watcher + LSP bridge: mount each project's root_path separately. We used to
# mount the whole repo root once, but that creates a mismatch for the
# project-d project whose project key is "project-d" but root_path is
# "project-d". Both containers expect /workspace/<project_key> regardless
# of root_path name — same mount set, reused for both (see
# docs/lsp-integration.md: the bridge needs the same
# static per-project bind mounts as the watcher, for the same reason).
WATCHER_MOUNTS=()
while IFS= read -r PROJECT_KEY; do
  ROOT_PATH=$(jq -r ".projects[\"$PROJECT_KEY\"].root_path" "$RESOLVED")
  if [ -n "$ROOT_PATH" ] && [ "$ROOT_PATH" != "null" ]; then
    PROJECT_ROOT_ABS="$REPO_ROOT/$ROOT_PATH"
  else
    PROJECT_ROOT_ABS="$REPO_ROOT"
  fi
  CONTAINER_ROOT="/workspace/${PROJECT_KEY}"
  WATCHER_MOUNTS+=("-v" "${PROJECT_ROOT_ABS}:${CONTAINER_ROOT}:ro")
done < <(jq -r '.projects | keys[]' "$RESOLVED")

# Java/Kotlin LSP servers each write their own project-local build state
# directly into the project directory — JDTLS's Maven importer needs to
# create <project>/target/, and kotlin-language-server's Gradle classpath
# resolution needs to create <project>/.gradle/ — regardless of any
# restore-step redirect, since these are separate, later steps the LSP
# servers run themselves (confirmed via live validation: both fail
# outright, not just degrade, if these paths aren't writable — see
# docs/lsp-integration.md). Since
# WATCHER_MOUNTS above binds each project read-only, we give these two
# specific subpaths their own writable tmpfs overlay instead — scoped
# per-project (only for projects that actually enable java/kotlin), not
# globally. Docker can only create a tmpfs mountpoint over a directory
# that already exists in the read-only bind-mount source, so we first
# ensure it exists on the host (idempotent — both paths are virtually
# universally gitignored already, same as any other build-tool output
# dir).
JAVA_KOTLIN_TMPFS_MOUNTS=()
while IFS= read -r PROJECT_KEY; do
  PROJECT_LANGUAGES=$(jq -r ".projects[\"$PROJECT_KEY\"].lsp.enabled_languages[]?" "$RESOLVED")
  [ -n "$PROJECT_LANGUAGES" ] || continue
  ROOT_PATH=$(jq -r ".projects[\"$PROJECT_KEY\"].root_path" "$RESOLVED")
  if [ -n "$ROOT_PATH" ] && [ "$ROOT_PATH" != "null" ]; then
    PROJECT_ROOT_ABS="$REPO_ROOT/$ROOT_PATH"
  else
    PROJECT_ROOT_ABS="$REPO_ROOT"
  fi
  CONTAINER_ROOT="/workspace/${PROJECT_KEY}"
  if echo "$PROJECT_LANGUAGES" | grep -qx "java"; then
    mkdir -p "${PROJECT_ROOT_ABS}/target"
    JAVA_KOTLIN_TMPFS_MOUNTS+=("--tmpfs" "${CONTAINER_ROOT}/target")
  fi
  if echo "$PROJECT_LANGUAGES" | grep -qx "kotlin"; then
    mkdir -p "${PROJECT_ROOT_ABS}/.gradle"
    JAVA_KOTLIN_TMPFS_MOUNTS+=("--tmpfs" "${CONTAINER_ROOT}/.gradle")
  fi
done < <(jq -r '.projects | keys[]' "$RESOLVED")

# ── Stop & remove any legacy per-project containers and old consolidated ones ─
# Only log this if something actually existed to remove — on a clean/first
# start there's nothing to report, and an unconditional "stopping..." line
# falsely implies containers were running when they weren't.
EXISTING_COUNT=$(
  set +e
  {
    docker ps -a --format "{{.Names}}" | grep -qx "$MCP_CONTAINER"     && echo "$MCP_CONTAINER"
    docker ps -a --format "{{.Names}}" | grep -qx "$WATCHER_CONTAINER" && echo "$WATCHER_CONTAINER"
    docker ps -a --format "{{.Names}}" | grep -qx "$LSP_CONTAINER"     && echo "$LSP_CONTAINER"
    docker ps -a --format "{{.Names}}" \
      | grep -E "^(.+_context_(mcp|watcher|lsp)|ai_context_(mcp|watcher|lsp)(_|$))"
    true
  } | sort -u | wc -l | tr -d ' '
)

if [ "$EXISTING_COUNT" -gt 0 ]; then
  ui_line "[start]" "Stopping $EXISTING_COUNT existing AI context container(s)..."
fi
# Remove the canonical names explicitly first, then the broader patterns
# (docker rm -f works on stopped containers too, so -a here for both the
# detection and removal keeps the reported count accurate).
docker rm -f "$MCP_CONTAINER" "$WATCHER_CONTAINER" "$LSP_CONTAINER" >/dev/null 2>&1 || true
docker ps -a --format "{{.Names}}" \
  | grep -E "^(.+_context_(mcp|watcher|lsp)|ai_context_(mcp|watcher|lsp)(_|$))" \
  | xargs -r -P0 docker rm -f >/dev/null 2>&1 || true

# ── Diagnostic: compact summary of what we are about to mount/expose ─────────
# Full detail (actual paths/ports) is only noise on a normal run — it's shown
# in full automatically if verify_mounts() ever detects a problem below.
ui_line "[start]" "Configured $(( ${#WATCHER_MOUNTS[@]} / 2 )) project mount(s), $(( ${#PORT_ARGS[@]} / 2 )) port mapping(s)."

# ── Verify that every expected project mount exists inside a container ────────
verify_mounts() {
  local row_idx="$1" container="$2"
  local missing=()
  while IFS= read -r PROJECT_KEY; do
    local expected="/workspace/${PROJECT_KEY}"
    if ! docker exec "$container" test -d "$expected" 2>/dev/null; then
      missing+=("$expected")
    fi
  done < <(jq -r '.projects | keys[]' "$RESOLVED")

  if [ ${#missing[@]} -ne 0 ]; then
    ui_set "$row_idx" failed "missing ${#missing[@]} mount(s)"
    ui_end
    echo "ERR The following project mount(s) are missing inside $container:"
    printf '  - %s\n' "${missing[@]}"
    echo "Actual mounts reported by docker inspect:"
    docker inspect --format '{{range .Mounts}}{{println .Type .Source "->" .Destination}}{{end}}' "$container"
    echo "Listing of /workspace inside the container:"
    docker exec "$container" ls -la /workspace 2>/dev/null || echo "  (could not list /workspace)"
    echo "ERR Stopping $container due to missing mounts."
    docker rm -f "$container" >/dev/null 2>&1 || true
    exit 1
  fi
  ui_set "$row_idx" done ""
}

# ── Live dashboard for the rest of the run ────────────────────────────────────
# From here on, every remaining step (image builds, container starts, mount
# verification) reports into one fixed, non-scrolling block of status lines
# (lib/ui.sh) instead of a long scroll of one-shot echo/BuildKit output — on
# a real terminal the block updates in place; anywhere else (CI, redirected
# logs) it falls back to plain sequential lines automatically.
ui_begin \
  "build ai-context-mcp" \
  "build ai-context-watcher" \
  "build ai-context-lsp" \
  "start mcp server" \
  "start watcher" \
  "start lsp bridge" \
  "verify watcher mounts" \
  "verify lsp bridge mounts"

# Images are always rebuilt from source so any source changes are picked up
# automatically — no manual `docker build` needed.
build_image_row 0 ai-context-mcp:latest     "$AI_CONTEXT_DIR/mcp-server/"
build_image_row 1 ai-context-watcher:latest "$AI_CONTEXT_DIR/watcher/"
build_image_row 2 ai-context-lsp:latest     "$AI_CONTEXT_DIR/lsp-bridge/" --build-arg LSP_LANGUAGES="$LSP_LANGUAGES"

# ── MCP container (consolidated) ──────────────────────────────────────────────
LSP_BRIDGE_URL="http://${LSP_CONTAINER}:8090"
ui_set 3 running ""
docker run -d \
  --name    "$MCP_CONTAINER" \
  --label   "com.docker.compose.project=ai-context" \
  --network ai-context-net \
  --restart unless-stopped \
  "${PORT_ARGS[@]}" \
  -v "${RESOLVED}:/app/.resolved.json:ro" \
  -e QDRANT_URL="http://ai_context_qdrant:6333" \
  -e POSTGRES_URL="postgresql://ai_context:ai_context_local@ai_context_postgres:5432/ai_context" \
  -e INFERENCE_URL="${INFERENCE_URL}" \
  -e RERANKER_TOP_N="${RERANKER_TOP_N}" \
  -e LSP_BRIDGE_URL="${LSP_BRIDGE_URL}" \
  ${OMLX_API_KEY:+-e OMLX_API_KEY="${OMLX_API_KEY}"} \
  ai-context-mcp:latest \
  node dist/multi_server.js \
  >/dev/null
MCP_URLS=""
while IFS= read -r PROJECT_KEY_URL; do
  PORT_URL=$(jq -r ".projects[\"$PROJECT_KEY_URL\"].mcp.port" "$RESOLVED")
  [ -n "$MCP_URLS" ] && MCP_URLS+=", "
  MCP_URLS+="${PROJECT_KEY_URL} → :${PORT_URL}/mcp"
done < <(jq -r '.projects | keys[]' "$RESOLVED")
ui_set 3 done "$MCP_URLS"

# ── Watcher container (consolidated) ──────────────────────────────────────────
ui_set 4 running ""
MCP_SERVER_ARGS=(
  -d
  --name "$WATCHER_CONTAINER"
  --label "com.docker.compose.project=ai-context"
  --network ai-context-net
  --restart unless-stopped
)
if [ "${#WATCHER_MOUNTS[@]}" -gt 0 ]; then
  MCP_SERVER_ARGS+=("${WATCHER_MOUNTS[@]}")
fi
if [ "${#GITIGNORE_MOUNTS[@]}" -gt 0 ]; then
  MCP_SERVER_ARGS+=("${GITIGNORE_MOUNTS[@]}")
fi
MCP_SERVER_ARGS+=(-v "${RESOLVED}:/app/.resolved.json:ro")
MCP_SERVER_ARGS+=(-e QDRANT_URL="http://ai_context_qdrant:6333")
MCP_SERVER_ARGS+=(-e POSTGRES_URL="postgresql://ai_context:ai_context_local@ai_context_postgres:5432/ai_context")
MCP_SERVER_ARGS+=(-e INFERENCE_URL="${INFERENCE_URL}")
[ -n "${OMLX_API_KEY:-}" ] && MCP_SERVER_ARGS+=(-e OMLX_API_KEY="${OMLX_API_KEY}")
MCP_SERVER_ARGS+=(ai-context-watcher:latest)
# Dockerfile ENTRYPOINT already invokes `python multi_entrypoint.py`.
# multi_entrypoint.py defaults to --container-root=/workspace, which matches
# the per-project mounts created above (/workspace/<project_key>).

# shellcheck disable=SC2068
docker run ${MCP_SERVER_ARGS[@]} >/dev/null
ui_set 4 done ""

# ── LSP bridge container (consolidated, one per repo — see
#    docs/lsp-integration.md) ─────────────────────────────
ui_set 5 running ""
LSP_ARGS=(
  -d
  --name "$LSP_CONTAINER"
  --label "com.docker.compose.project=ai-context"
  --network ai-context-net
  --restart unless-stopped
)
if [ "${#WATCHER_MOUNTS[@]}" -gt 0 ]; then
  LSP_ARGS+=("${WATCHER_MOUNTS[@]}")
fi
if [ "${#JAVA_KOTLIN_TMPFS_MOUNTS[@]}" -gt 0 ]; then
  LSP_ARGS+=("${JAVA_KOTLIN_TMPFS_MOUNTS[@]}")
fi
LSP_ARGS+=(-v "${RESOLVED}:/app/.resolved.json:ro")

# ── Dependency cache mounts (see docs/lsp-integration.md) ──
# Read-write mounts of the shared volumes created above, at each ecosystem's
# default cache-path env var — read-write is safe here because these are
# named Docker volumes, not bind mounts into the user's real repo (unlike
# WATCHER_MOUNTS above, which must stay read-only).
case ",$LSP_LANGUAGES," in
  *,go,*) LSP_ARGS+=(-v ai_context_go_mod_cache:/dep-cache/go -e GOMODCACHE=/dep-cache/go) ;;
esac
case ",$LSP_LANGUAGES," in
  *,rust,*) LSP_ARGS+=(-v ai_context_cargo_registry:/dep-cache/cargo -e CARGO_HOME=/dep-cache/cargo) ;;
esac
case ",$LSP_LANGUAGES," in
  *,csharp,*) LSP_ARGS+=(-v ai_context_nuget_packages:/dep-cache/nuget -e NUGET_PACKAGES=/dep-cache/nuget) ;;
esac
case ",$LSP_LANGUAGES," in
  *,ruby,*) LSP_ARGS+=(-v ai_context_gem_cache:/dep-cache/gems -e BUNDLE_PATH=/dep-cache/gems) ;;
esac
case ",$LSP_LANGUAGES," in
  *,dart,*) LSP_ARGS+=(-v ai_context_pub_cache:/dep-cache/pub -e PUB_CACHE=/dep-cache/pub) ;;
esac
case ",$LSP_LANGUAGES," in
  *,python,*) LSP_ARGS+=(-v ai_context_pip_cache:/dep-cache/pip -e PIP_CACHE_DIR=/dep-cache/pip/wheels) ;;
esac
case ",$LSP_LANGUAGES," in
  *,swift,*) LSP_ARGS+=(-v ai_context_swiftpm_cache:/dep-cache/swiftpm) ;;
esac
case ",$LSP_LANGUAGES," in
  *,java,*|*,kotlin,*)
    LSP_ARGS+=(-v ai_context_maven_repository:/root/.m2)
    LSP_ARGS+=(-v ai_context_gradle_cache:/root/.gradle)
    ;;
esac

LSP_ARGS+=(ai-context-lsp:latest)
# No host port published — only the MCP server (same ai-context-net network,
# via LSP_BRIDGE_URL above) ever talks to this container.

# shellcheck disable=SC2068
docker run ${LSP_ARGS[@]} >/dev/null
ui_set 5 done ""

ui_set 6 running ""
verify_mounts 6 "$WATCHER_CONTAINER"

ui_set 7 running ""
verify_mounts 7 "$LSP_CONTAINER"

ui_end
ui_line_reset  # dashboard block printed a lot below the last ui_line call
echo "[start] Done. All containers are up and mounts verified."
