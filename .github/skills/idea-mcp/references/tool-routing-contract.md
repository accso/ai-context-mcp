---
name: tool-routing-contract
description: "Single-page, non-negotiable tool routing table for this repo. Read this FIRST, before explore.md/edit.md/ops.md — those give detail, this gives the binding decision per action type."
---

# Tool Routing Contract

This is a decision table, not background reading. Before any action below, route to the named tool. "Default tool" =
`view`/`grep`/`glob`/`bash`/`edit`/`create`
(the generic, non-IDE tools always available in this environment).

| Action                                                                                      | Required tool                                                                                                | Default tool allowed?                                |
|---------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------|------------------------------------------------------|
| Read a file's contents                                                                      | `read_file`                                                                                                    | No                                                   |
| Search text/regex/symbol in the project                                                     | `search_text` / `search_regex` / `search_symbol`                                                              | No                                                   |
| Find files by name/glob                                                                     | `search_file`                                                                                                  | No                                                   |
| Edit an existing file                                                                       | `apply_patch` (this session's schema does not expose a separate replace-text tool - confirmed live, not assumed) | No                                                   |
| Create a new file                                                                           | `create_new_file`                                                                                              | No                                                   |
| Rename/refactor a symbol                                                                    | `rename_refactoring` (or other refactoring tool)                                                               | No                                                   |
| Reformat a file                                                                             | `reformat_file`                                                                                                | No                                                   |
| Check compile errors/warnings                                                               | `get_file_problems`                                                                                            | No                                                   |
| Run a shell command, script, or test                                                        | `execute_terminal_command`                                                                                     | No                                                   |
| Build the project                                                                           | `build_project`                                                                                                | No                                                   |
| Run a run-configuration                                                                     | `execute_run_configuration`                                                                                    | No                                                   |
| Query/inspect a database                                                                    | `execute_sql_query` / `introspect_schema` / `preview_table_data`                                               | No                                                   |
| Check VCS status                                                                            | `git_status`                                                                                                   | No                                                   |
| **git add / commit / other VCS mutation**                                                   | **No dedicated tool exists for this.** Use `execute_terminal_command` to run `git`, not the raw `bash` tool.   | Exception — terminal only, never the raw `bash` tool |
| Broad, non-targeted "what does this module do" exploration with no concrete file/symbol yet | `ai-context-context` MCP (`repo_structure` → `semantic_search` → `get_file`)                                   | No                                                   |
| Non-IDE-project research (fetching external docs, license files, web pages)                 | `web_fetch`                                                                                                    | Yes — this is genuinely outside IDE/ai-context scope |

Note: this session's live schema exposes tool names with **no `idea_`/`ai-context-`-style prefix** (e.g. bare `read_file`, `search_text`, `git_status`) — invoked via the `idea-mcp-execute_tool` dispatcher's `command` string. Tool names in this table were corrected against that live schema (see `.ai-context` MCP session, 2026-08-13) after several documented names (`idea_get_file_text_by_path`, `idea_replace_text_in_file`, `idea_find_files_by_name_keyword`, `idea_find_files_by_glob`, `idea_search_in_files_by_text/regex`, `idea_find_commit_by_message`, `idea_get_project_vcs_status`) turned out not to exist. Always re-verify against the live schema error message if a call fails — naming can still drift by IDE/plugin version.

**Tool names are not the only thing that drifts — parameter names do too, and a wrong name here fails differently: not "unknown tool" but `Missing required parameters: <name>"`, which looks like a usage mistake rather than a stale doc.** Confirmed live 2026-08-13: `search_text`/`search_regex`/`search_symbol`/`search_file` all take **`q`**, not `query`/`pattern`/`globPattern` as earlier docs claimed; `read_file` and `get_file_problems` take **`file_path`** (snake_case), not `filePath`. Other tools in this skill (`apply_patch`, `reformat_file`, refactorings, `xdebug_*`) still document `filePath` (camelCase) and have NOT been re-verified per-parameter — treat any undocumented-as-tested parameter name as a guess, and if a call fails with "Missing required parameters: X", that error message tells you the exact correct name to retry with immediately, rather than reverting to a non-IDE tool.

## Enforcement mechanism (not just wording)

A text table alone does not prevent drift over a long task — this repo's existing `idea-mcp/SKILL.md` already said
"never fall back" and that got violated mid-task anyway. The actual enforcement is external and inspectable, not
advisory:

1. **Before starting a batch of actions**, log the planned tool for each step into the session's `tool_ledger` SQL table
   (see below) — not after the fact.
2. **After finishing a batch**, mark each row's `actual_tool` column from what was really called. A mismatch between
   `required_tool` and
   `actual_tool` is a visible, queryable fact — not something that depends on self-report at the end of a long
   conversation.
3. **The user (or the agent itself, mid-task) can query this table at any time** via the `sql`/`session_store_sql` tool
   to audit compliance:
   ```sql
   SELECT * FROM tool_ledger WHERE required_tool != actual_tool;
   ```
4. This is re-checked at every phase change (explore → edit → test/build → VCS), not just once at task start.

## Schema

```sql
CREATE TABLE IF NOT EXISTS tool_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action        TEXT NOT NULL,        -- short description of the step
    required_tool TEXT NOT NULL,        -- from the table above
    actual_tool   TEXT,                 -- filled in after the call is made
    note          TEXT,                 -- e.g. "exception: no idea_* equivalent"
    created_at    TEXT DEFAULT (datetime('now'))
);
```
