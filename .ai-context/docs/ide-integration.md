# IDE Integration — Full Reference

`init.sh` — run automatically by `mise run ai-context-up` on first use — writes the MCP server entries to every path configured in `shared.mcp_config_paths`. The default set covers GitHub Copilot's native VS Code plugin and IntelliJ plugin, so no manual steps are needed for those. Any other MCP-compliant agent (e.g. Crush) can be pointed at the same URL manually — see "Any other MCP client" below.

For the quick auto-config table, see [`README.md`](../README.md#ide-integration). This doc adds instruction-delivery details, skill-file placement per agent, and manual setup for any other MCP client.

> **`sst/opencode` users:** everything below is the MCP-client setup path
> (writing `mcp.json`/skill files). `sst/opencode` doesn't need any of
> it — install **[`opencode-ai-context-plugin/`](../../opencode-ai-context-plugin/)**
> instead, a native plugin wired directly into `sst/opencode`'s own
> tool/hook API (no `mcp.json` entry, no skill file — its tools are
> self-describing, see
> [ADR-0010](../../opencode-ai-context-plugin/docs/adr/0010-no-skill-md-self-describing-tool-descriptions.md)).
> Only fall back to this Docker stack's MCP path for `sst/opencode` if you
> specifically need "heavy mode" (see the plugin's README).

## Auto-configured clients

| Client | Default config file | Scope |
|-----|-------------|-------|
| GitHub Copilot — native VS Code plugin | `<repo-root>/.vscode/mcp.json` | Project — commit this file so the whole team gets it |
| GitHub Copilot — IntelliJ plugin (also used by JetBrains AI Assistant setups) | `~/.config/github-copilot/intellij/mcp.json` | Global — all projects share this file |

Use the `shared.mcp_config_paths` list (and a per-project `projects[].mcp_config_paths` override if needed) to add more destinations or remove the defaults. Paths accept `{repo_root}`, a leading `~` for `$HOME`, and the standard project placeholder `{project_name}` in filenames. The entry added to each file looks like:

```json
{
  "servers": {
    "<project-name>-context": {
      "type": "http",
      "url": "http://localhost:<port>/mcp"
    }
  }
}
```

Re-running init is safe — it updates each project's entry without touching unrelated entries, and it writes to every configured `mcp_config_paths` entry.

**After init, restart your IDE** (or toggle the Copilot chat panel) once to pick up the new server.

## Customising AI instructions

Instructions reach the agent through three paths — in order of reliability:

| Path | Who reads it | When |
|------|-------------|------|
| **MCP `initialize` response** (`instructions` field) | Any MCP client supporting spec 2025-03-26 — e.g. GitHub Copilot | Automatically, during handshake — **computed once when the server process starts** and reused for every connection until the container restarts |
| **Skill file** (`.ai-context/skills/ai-context-workflow/SKILL.md`) | Agents with skill discovery (e.g. Crush) — description-matched, not filename-matched | Automatically, when the skill's `description` matches the task |
| **`GET /instructions`** REST endpoint | Humans, scripts, CLI | On explicit request — **re-reads the file fresh on every call**, unlike the handshake path |

`AGENTS.md` at the repo root lists every project's skill name and MCP server key as the primary, cross-agent entry point — it is read natively by Crush and increasingly by other coding agents, and is the current replacement for the removed Copilot-specific `.github/copilot-instructions.md`.

Both paths serve the same *content* (the generated default or the contents of `.ai-context/instructions.md`), computed by the same `loadInstructions()` function — but **not with the same freshness**: the handshake path only calls it once, at server startup; the REST path calls it again on every request. Editing `.ai-context/instructions.md` while the server is already running is picked up immediately by `GET /instructions`, but **not** by the MCP handshake `instructions` field until the MCP container restarts.

**Default:** if no custom file exists a generated document is served automatically — always accurate, based on live config, and kept short (a pointer to the skill file, not a duplicate of its full workflow text).

**Custom:** create `.ai-context/instructions.md` in the repository root. It is mounted read-only into every MCP container at startup. Two placeholders are substituted automatically:

| Placeholder | Replaced with |
|-------------|---------------|
| `{project_name}` | The project's `name` from `config.yaml` |
| `{display_name}` | The project's `display_name` from `config.yaml` |

```markdown
# {display_name} — AI Context Instructions

See the repo-wide skill `ai-context-workflow` for the full workflow.

## Module ownership
- `bar/` — owned by Team Alpha
- `foo/` — owned by Team Beta
```

Access the current instructions for any project at `http://localhost:<port>/instructions` — always current (re-read per request). The MCP handshake copy only updates on the next container restart (see freshness note above).

## Skill file placement per agent

`scripts/init_ide.sh` always writes the canonical skill file to
`.ai-context/skills/ai-context-workflow/SKILL.md` — already in the
folder-per-skill, `SKILL.md`-named format that skill-discovery agents expect.
**This canonical copy is an example/starter file only — no agent reads
skills from inside `.ai-context/` directly.** Copy it (or configure
`shared.skill.target_path` / `shared.skill.target_paths` below to have `init_ide.sh`
do the copy for you) into whatever directory your tooling actually scans for skills.

| Agent | Convention |
|-------|-----------|
| Crush (successor to OpenCode, same author/team) | Project-relative `.crush/skills/<name>/SKILL.md`, or global `~/.config/crush/skills/<name>/SKILL.md`; also scans `.agents/skills/`, `.claude/skills/`, `.cursor/skills/` (project) and `~/.config/agents/skills/`, `~/.agents/skills/`, `~/.claude/skills/` (global). Extra directories via `option skill-path "<dir>"`. |
| Other agents with skill discovery | Copy `.ai-context/skills/ai-context-workflow/SKILL.md` into your agent's `skills/<name>/` folder manually, following its own convention |

Set `shared.skill.target_path` (single directory) or `shared.skill.target_paths` (list of
directories), or the per-project `projects[].skill.target_path` / `projects[].skill.target_paths`
overrides, in `config.yaml` to have `init_ide.sh` copy the rendered skill there automatically on
every run. Config values are **directories only**; `init_ide.sh` always places the skill at
`<skill_save_dir>/skills/ai-context-workflow/SKILL.md`. A leading `~` expands to `$HOME`,
relative paths are resolved from the repo root, and `{repo_root}` is also supported. A
trailing slash is optional.

We recommend standardizing on **`.github/`** as one copy target — it isn't owned by any one
agent, so it works as a single shared location a team can point every agent's own
skill-discovery config at, e.g.:

```yaml
skill:
  target_path: ".github"
```

Or copy to several agents at once:

```yaml
skill:
  target_paths:
    - "~/.config/crush/skills"
    - ".github"
```

`init_ide.sh` will create `.github/skills/ai-context-workflow/SKILL.md` and/or
`~/.config/crush/skills/ai-context-workflow/SKILL.md` automatically.

Leave `target_path`/`target_paths` empty (default) to only write the canonical copy and place
it yourself. Note that Crush does not scan `.github/skills/` by default — either add
`.github` via `option skill-path ".github"`, or additionally copy into one of Crush's own
scanned paths (e.g. `.crush/skills/<name>/SKILL.md`, `.agents/skills/<name>/SKILL.md`)
if you want Crush itself to auto-discover skills copied there.

## Any other MCP client

For a coding agent that doesn't read either of the config files listed in
"Auto-configured clients" above (i.e. not GitHub Copilot's VS Code or IntelliJ plugin), add the
MCP server manually using whatever mechanism that agent provides for custom MCP
servers:

1. Find the agent's MCP server settings (usually a settings UI or its own config file).
2. Add a new server pointing at `http://localhost:<port>/mcp` — use your project's configured port.
3. Once connected, the agent will call `repo_structure` / `semantic_search` / `get_file` itself as needed, guided by the MCP handshake instructions and/or skill file (see above).

> **Note:** The MCP server is only reachable while `mise run ai-context-up` is running.
> Inactive entries in an agent's config are silently ignored.
