# AI Context Instructions

This MCP server provides code context for the **{display_name}** project (key: `{project_name}`).

See the repo-wide skill **`ai-context-workflow`** (generated at
`.ai-context/skills/ai-context-workflow/SKILL.md`) for the full workflow.

In short:

1. `repo_structure` — call first for orientation. Per-file entries may show
   `[hub]`/`[bridge]` tags (structural role, derived from the import/call
   graph) — hubs are worth extra care before changing, bridges connect
   otherwise-separate parts of the codebase.
2. `semantic_search(query)` — use technical identifiers where possible. Each
   hit includes a `why` note explaining how it was found/scored (identifier
   match, vector similarity, keyword boost, or rerank) — use it to judge
   result trustworthiness, not just the raw score.
3. `get_file(file_path, [start_line], [end_line])` — only if step 2 wasn't enough.
4. `analyze_impact(symbol_name, direction, [file_path], [depth], [include_tests])` —
   before editing/removing a symbol, to see its callers/callees (blast radius)
   and which test files likely cover it. `direction` is `"callers"`, `"callees"`,
   or `"both"`. Pass `file_path` to disambiguate if the symbol name is not unique
   in the project.
5. `resolve_symbol(file_path, line, character, kind)` — only when you need
   compiler-grade certainty that `analyze_impact`'s heuristic call graph can't
   give you (e.g. confirming the exact definition/references across dynamic
   dispatch, generics, or re-exports). `kind` is `"definition"`, `"references"`,
   or `"hover"`. Slower (spawns a real language server) and only available for
   projects/languages with `lsp.enabled_languages` configured — if it reports
   the language isn't enabled or isn't configured, fall back to
   `analyze_impact`/`semantic_search` instead of treating it as an error.

Stop as soon as you have enough context — don't call every tool by default.
