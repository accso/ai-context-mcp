#!/usr/bin/env bash
# init_ide.sh — Register all projects from .resolved.json into IDE MCP configs,
# render each project's canonical skill file, and update AGENTS.md.
#
# Reads every project from .resolved.json and merges their MCP server entries
# into configurable mcp.json paths. Also writes the skill file to every
# configured skills directory so each agent/tooling convention can find it.
#
# Safe to run multiple times — idempotent. Unrelated entries are preserved.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_CONTEXT_DIR="$(dirname "$SCRIPT_DIR")"
RESOLVED="$AI_CONTEXT_DIR/.resolved.json"

require_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERR '$1' not found. Install: $2"; exit 1; }; }
require_cmd jq "brew install jq"

if [ ! -f "$RESOLVED" ]; then
  echo "ERR .resolved.json not found. Run: mise run ai-context-up"
  exit 1
fi

REPO_ROOT=$(jq -r '.repo_root' "$RESOLVED")

# ── Track whether anything actually changed this run ──────────────────────────
# Every write below is idempotent already (same content in -> same content
# out), but previously every step unconditionally echoed "OK ..." even when
# nothing changed — noisy on every single `ai-context-up` run. Now: only log
# a line when a file's content actually changes; if nothing changed at all,
# print one compact summary line instead.
CHANGED_ITEMS=()
_log_change() { CHANGED_ITEMS+=("$1"); echo "    OK $1"; }

# ── Helper: collect a list of values (string or list) from resolved JSON ──────
# Usage: _string_or_list '.mcp_config_paths'  -> emits one path per line
_string_or_list() {
  jq -r "$1 // empty | if type == \"array\" then .[] else . end" "$RESOLVED"
}

# ── Default + configured mcp.json save paths ──────────────────────────────────
# Config key: shared.mcp_config_paths  (string or list of strings)
# Placeholders: {project_name} is replaced with each project key.
default_mcp_paths() {
  printf '%s\n' "$REPO_ROOT/.vscode/mcp.json"
  printf '%s\n' "$REPO_ROOT/.github/mcp.json"
  printf '%s\n' "$HOME/.config/github-copilot/intellij/mcp.json"
}

MCP_PATHS=()
while IFS= read -r raw; do
  [ -n "$raw" ] || continue
  # Expand placeholders manually instead of eval for safety.
  # {project_name} is a per-project placeholder, but mcp.json paths are shared
  # across projects — only {repo_root} and env-style $HOME are useful here.
  expanded="${raw//\{repo_root\}/$REPO_ROOT}"
  expanded="${expanded/#\~/$HOME}"
  [ -n "$expanded" ] || continue
  if [ ${#MCP_PATHS[@]} -gt 0 ]; then
    duplicate=false
    for existing in "${MCP_PATHS[@]}"; do
      if [ "$existing" = "$expanded" ]; then
        duplicate=true
        break
      fi
    done
    if [ "$duplicate" = false ]; then
      MCP_PATHS+=("$expanded")
    fi
  else
    MCP_PATHS+=("$expanded")
  fi
done < <({ _string_or_list '.mcp_config_paths'; default_mcp_paths; })

if [ ${#MCP_PATHS[@]} -eq 0 ]; then
  echo "ERR no mcp.json paths configured."
  exit 1
fi

# ── Merge one server entry into a target mcp.json ────────────────────────────
# GitHub's mcp.json (.github/mcp.json) expects the top-level key "mcpServers",
# while VS Code / IntelliJ mcp.json files expect "servers".
_top_level_key_for_target() {
  case "$1" in
    */.github/mcp.json) printf '%s\n' "mcpServers" ;;
    *) printf '%s\n' "servers" ;;
  esac
}

_merge_server() {
  local target="$1" key="$2" url="$3"
  local top_key
  top_key=$(_top_level_key_for_target "$target")
  mkdir -p "$(dirname "$target")"
  local tmp
  tmp=$(mktemp)
  if [ -f "$target" ]; then
    jq --arg tk "$top_key" --arg k "$key" --arg u "$url" \
      '.[$tk][$k] = {"type": "http", "url": $u}' \
      "$target" > "$tmp"
    if cmp -s "$tmp" "$target"; then
      rm -f "$tmp"
      return 0
    fi
  else
    jq -n --arg tk "$top_key" --arg k "$key" --arg u "$url" \
      '{($tk): {($k): {"type": "http", "url": $u}}}' > "$tmp"
  fi
  mv "$tmp" "$target"
  _log_change "MCP config updated: $target ($key)"
}

# ── Render the repo-wide canonical skill file (§7.1 template) ───────────────
# Writes one skill that covers every project registered in .resolved.json,
# identifying the correct {project_key}-context MCP server based on the user's
# question. This replaces the previous one-skill-per-project layout.
_render_skill() {
  local skills_dir="$AI_CONTEXT_DIR/skills/ai-context-workflow"
  mkdir -p "$skills_dir"
  local canonical="$skills_dir/SKILL.md"

  local project_table=""
  while IFS= read -r project_key; do
    local display_name port
    display_name=$(jq -r ".projects[\"$project_key\"].display_name" "$RESOLVED")
    port=$(jq -r ".projects[\"$project_key\"].mcp.port" "$RESOLVED")
    project_table="${project_table}   | \`${project_key}-context\` | ${display_name} | http://localhost:${port}/mcp |
"
  done < <(jq -r '.projects | keys[]' "$RESOLVED")

  local repo_root_basename
  repo_root_basename=$(basename "$REPO_ROOT")

  local content
  content=$(cat <<SKILL
---
name: ai-context-workflow
description: >
  Use for code context, file locations, or an architecture overview on the
  ${repo_root_basename} repository before writing or modifying code. Drives
  repo_structure, semantic_search, get_file, analyze_impact, and (when
  configured) resolve_symbol against the project-specific
  {project_key}-context MCP server that matches the user's question.
mcp_server: any registered *-context MCP server (see workflow below)
tools: [repo_structure, semantic_search, get_file, analyze_impact, resolve_symbol]
---

# ${repo_root_basename} — Codebase Context Workflow

This skill covers every project indexed by ai-context in this repository.
Each project is served by a separate MCP server named \`{project_key}-context\`.

1. **Pick the project.** Choose the MCP server whose project best matches the
   user's question. Registered projects:

   | MCP server | Project | URL |
   |------------|---------|-----|
${project_table}   If the question is repo-wide or you are unsure, start with the project that
   owns the file path or module mentioned in the question.

2. \`repo_structure\` — call first on the chosen project for orientation
   (module map, directory tree). Per-file entries may show
   \`[hub]\`/\`[bridge]\` structural-role tags (derived from the import/call
   graph) — hubs warrant extra care before changing.

3. \`semantic_search(query)\` — use technical identifiers, not prose
   (e.g. \`UserRepository authenticate\`, not "how do users log in").
   Read the returned summaries before requesting full files. Each hit
   includes a \`why\` note (identifier match, vector similarity, keyword
   boost, or rerank) — use it to judge trustworthiness, not just the score.

4. \`get_file\` — only if step 3 wasn't enough. Max 2 calls per query,
   max 3 files per call. Works for code and docs automatically.

5. \`analyze_impact(symbol_name, direction, [file_path], [depth],
   [include_tests])\` — before editing or removing a symbol, to see its
   callers/callees (blast radius) and which test files likely cover it.
   \`direction\` is \`callers\`, \`callees\`, or \`both\`. Pass \`file_path\` to
   disambiguate if the symbol name is not unique in the project.

6. \`resolve_symbol(file_path, line, character, kind)\` — only if the project
   has \`lsp\` enabled for that language (not every project/language has
   this). Use for compiler-grade precision (\`kind=definition\`,
   \`references\`, or \`hover\`) when the heuristic call graph from
   \`analyze_impact\` or the results from \`semantic_search\`/\`get_file\` leave
   the exact declaration or call sites ambiguous. Higher cost than steps 2
   to 5 — reach for it only when those are not enough, and skip it if the
   tool reports the language or project is not configured for it.

Some projects declare \`sources\` in ai-context config; if the answer likely
lives in a related module (e.g. a frontend asking about a backend API),
repeat the workflow against the source project.

Stop as soon as you have enough context — don't call every tool by default.
If \`semantic_search\` finds nothing after one rephrase, say so instead of
guessing.
SKILL
)

  local tmp_canonical
  tmp_canonical=$(mktemp)
  printf '%s\n' "$content" > "$tmp_canonical"
  if [ -f "$canonical" ] && cmp -s "$tmp_canonical" "$canonical"; then
    rm -f "$tmp_canonical"
  else
    mv "$tmp_canonical" "$canonical"
    _log_change "skill: $canonical"
  fi

  # Write the canonical skill to every configured skills save directory.
  # Config key: shared.skill.target_paths (string or list) and legacy
  # shared.skill.target_path. These are directories only; the actual skill is
  # always placed at <dir>/skills/ai-context-workflow/SKILL.md.
  while IFS= read -r raw_target; do
    [ -n "$raw_target" ] || continue
    # shellcheck disable=SC2016
    local target="${raw_target//\{repo_root\}/$REPO_ROOT}"
    target="${target/#\~/$HOME}"
    if [[ "$target" != /* ]]; then
      target="$REPO_ROOT/$target"
    fi
    # Normalise: ensure exactly one trailing slash, then append the fixed
    # folder-per-skill path for the repo-wide skill.
    target="${target%/}/skills/ai-context-workflow/SKILL.md"
    mkdir -p "$(dirname "$target")"
    if [ -f "$target" ] && printf '%s\n' "$content" | cmp -s - "$target"; then
      continue
    fi
    printf '%s\n' "$content" > "$target"
    _log_change "skill target: $target"
  done < <({
    _string_or_list '.skill.target_paths'
    _string_or_list '.skill.target_path'
  } | sort -u)

  # Remove stale per-project skill directories that this script used to create.
  # (Per-project skill files outside .ai-context/skills are left untouched.)
  while IFS= read -r project_key; do
    local stale_dir="$AI_CONTEXT_DIR/skills/${project_key}-context-workflow"
    if [ -d "$stale_dir" ]; then
      rm -rf "$stale_dir"
      _log_change "removed stale skill dir: $stale_dir"
    fi
  done < <(jq -r '.projects | keys[]' "$RESOLVED")
}

# ── Mirror IntelliJ AI Assistant rules into GitHub Copilot instructions ──────
# `.aiassistant/rules/*.md` is the canonical source for repo-wide agent rules
# (plan-before-implementing, IDE MCP usage, etc.). GitHub Copilot discovers the
# same rules under a different convention: `.github/instructions/*.instructions.md`.
# Content is identical; only the file name/suffix and directory differ. Mirror
# every rule file so the two stay in sync without manual copy-pasting.
_render_rules() {
  local rules_src="$REPO_ROOT/.aiassistant/rules"
  local rules_dest="$REPO_ROOT/.github/instructions"

  if [ ! -d "$rules_src" ]; then
    return 0
  fi

  mkdir -p "$rules_dest"
  local found=false
  for rule_file in "$rules_src"/*.md; do
    [ -e "$rule_file" ] || continue
    found=true
    local base
    base=$(basename "$rule_file" .md)
    local target="$rules_dest/${base}.instructions.md"
    if [ -f "$target" ] && cmp -s "$rule_file" "$target"; then
      continue
    fi
    cp "$rule_file" "$target"
    _log_change "rule mirrored: $target"
  done
  if [ "$found" = false ]; then
    return 0
  fi
}

# ── Update AGENTS.md "## AI Context" section (idempotent marker-based merge) ─
_update_agents_md() {
  local agents_md="$REPO_ROOT/AGENTS.md"
  local marker="## AI Context"
  local tmp_section
  tmp_section=$(mktemp)

  {
    printf '%s\n\n' "$marker"
    printf 'This repository uses local AI context MCP servers — one per project below.\n'
    printf 'Each server provides indexed code context (module maps, ranked search, file content)\n'
    printf 'scoped exclusively to its project. Use the single repo-wide skill\n'
    printf '`ai-context-workflow` to route queries to the right project server.\n\n'
    printf '| Skill | MCP server | When to use |\n'
    printf '|-------|------------|-------------|\n'
    printf '| `ai-context-workflow` | any registered *-context server | Code context / architecture across the repo |\n'
    while IFS= read -r project_key; do
      local display_name
      display_name=$(jq -r ".projects[\"$project_key\"].display_name" "$RESOLVED")
      printf '| `%s-context-workflow` (deprecated) | `%s-context` | Old per-project skill; prefer `ai-context-workflow` |\n' \
        "$project_key" "$project_key"
    done < <(jq -r '.projects | keys[]' "$RESOLVED")
    printf '\n'
  } >> "$tmp_section"

  if [ ! -f "$agents_md" ]; then
    printf '# Repository Agent Instructions\n\n' > "$agents_md"
    cat "$tmp_section" >> "$agents_md"
    _log_change "created $agents_md"
  elif grep -qF "$marker" "$agents_md"; then
    local tmp_out
    tmp_out=$(mktemp)
    awk \
      -v section_file="$tmp_section" \
      -v heading="$marker" \
      'BEGIN { inside=0 }
       $0 == heading {
         while ((getline line < section_file) > 0) print line
         close(section_file)
         inside=1; next
       }
       inside && /^## / && $0 != heading { inside=0 }
       !inside { print }' \
      "$agents_md" > "$tmp_out"
    if cmp -s "$tmp_out" "$agents_md"; then
      rm -f "$tmp_out"
    else
      mv "$tmp_out" "$agents_md"
      _log_change "updated \"$marker\" section in $agents_md"
    fi
  else
    printf '\n' >> "$agents_md"
    cat "$tmp_section" >> "$agents_md"
    _log_change "appended \"$marker\" section to $agents_md"
  fi
  rm -f "$tmp_section"
}

# ── Register every project ────────────────────────────────────────────────────
COUNT=0
while IFS= read -r project_key; do
  PORT=$(jq -r ".projects[\"$project_key\"].mcp.port" "$RESOLVED")
  SERVER_KEY="${project_key}-context"
  SERVER_URL="http://localhost:${PORT}/mcp"

  for target in "${MCP_PATHS[@]}"; do
    _merge_server "$target" "$SERVER_KEY" "$SERVER_URL"
  done

  COUNT=$((COUNT + 1))
done < <(jq -r '.projects | keys[]' "$RESOLVED")

# Render the single repo-wide skill (replaces the previous per-project skills).
_render_skill

# Mirror .aiassistant/rules/*.md into .github/instructions/*.instructions.md.
_render_rules

# ── Update repo-root AGENTS.md ─────────────────────────────────────────────────
_update_agents_md

# ── Summary ────────────────────────────────────────────────────────────────────
# Every _log_change call above already printed its own "OK ..." line as it
# happened; here we only add one final compact line so a normal (no-op) run
# doesn't look silent/uncertain, and a changed run gets a clear count instead
# of forcing the reader to count the OK lines themselves.
if [ ${#CHANGED_ITEMS[@]} -eq 0 ]; then
  echo "[init_ide] IDE integration up to date — $COUNT MCP server(s) in ${#MCP_PATHS[@]} config(s), no changes."
else
  echo "[init_ide] $COUNT MCP server(s) in ${#MCP_PATHS[@]} config(s) — ${#CHANGED_ITEMS[@]} file(s) updated (see above)."
fi


