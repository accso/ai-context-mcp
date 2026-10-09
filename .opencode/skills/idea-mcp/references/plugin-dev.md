# Plugin & Monorepo DevKit

Narrow, specialist toolsets for IntelliJ Platform plugin development and the JetBrains monorepo. Only relevant when the project under work is itself a JetBrains/IntelliJ plugin or the platform monorepo — not for ordinary application code.

## Toolsets covered
- **DevKit MCP toolset** - plugin-development helpers: locating/analyzing extension points, plugin.xml structure, platform API usage, read/write-lock and UI-thread ("EDT") requirement analysis for a method under the cursor.
- **Monorepo DevKit MCP toolset** - navigation and analysis helpers scoped for the scale of the JetBrains platform monorepo (many modules across the codebase).

## Activation Gate (tool availability and project applicability)
Use this guidance only when **both** conditions hold:
1. At least one DevKit tool is available in the current MCP session (e.g. `idea_analyze_threading_requirements`, or another `idea_devkit_*`/`idea_monorepo_*`-named tool exposed by the live schema).
2. The project under work is confirmed to actually be an IntelliJ-platform plugin or the JetBrains monorepo (look for `plugin.xml`, a dependency on `intellij-platform`/`intellij.platform.*` artifacts, or the repo being the JetBrains monorepo).

If either condition fails, state the blocker explicitly and fall back to `references/explore.md`/`references/edit.md` instead — DevKit tools produce heuristic, sometimes unreliable results outside a genuine plugin/platform project, and calling them elsewhere wastes a round trip.

## Tool reference

| Situation | Tool | Key parameters |
|---|---|---|
| Analyze UI-thread ("EDT") / read-write-lock requirements for the method under the caret, with call paths | `idea_analyze_threading_requirements` | `filePath`, `line`, `column` (all required, 1-based, identify the method under the caret), `projectPath?` |
| Locate/analyze extension point usages, plugin.xml structure | not consistently documented publicly — check the live MCP tool list for names prefixed `idea_devkit_*`; typical parameters follow the same `filePath`+`line`+`column` or `projectPath`-only pattern as other `idea_*` tools |
| Monorepo-scale navigation/analysis helpers | not consistently documented publicly — check the live MCP tool list for `idea_monorepo_*`-style names; expect `projectPath?` plus a query/glob-style filter parameter |

Exact DevKit/Monorepo DevKit tool names and parameters are less consistently documented publicly than the core toolsets — always confirm the current name and full parameter list against the live tool schema exposed by your MCP client before calling, rather than assuming the names above are exhaustive.

`idea_analyze_threading_requirements` (and any other call-path/lock/thread tool) returns **heuristic** results: call paths may be unreachable in practice and coverage may be incomplete. Treat its output as a lead to investigate, not a proven fact — confirm suspicious findings by reading the actual code (`references/explore.md`) or, if runtime confirmation is needed, via `references/debugger.md`.

## Workflow
1. **Confirm applicability first.** Re-check the Activation Gate above before calling anything in this reference.
2. **Treat call-path/lock/thread analysis as heuristic.** Use it to narrow investigation, then confirm with a real read or a debugger session — never assert a threading violation from this tool's output alone.
3. **Position precisely.** These tools operate on "the method under the caret" via `filePath` + 1-based `line`/`column` — get exact coordinates from a prior search/read (`references/explore.md`) before calling.
4. **Combine with general toolsets.** DevKit findings (e.g. a UI-thread requirement) typically inform an edit or refactor — hand off to `references/edit.md` once you know what to change.

## Argument hygiene
- `line`/`column` are 1-based and must come from a prior `references/explore.md` result, never guessed.
- Pass `projectPath` when known, especially in monorepo sessions where multiple modules/projects may be open simultaneously.
- Do not fabricate placeholder values for optional parameters; omit them instead.

## When NOT to use this guidance
- Ordinary application/service code with no plugin/platform dependency -> `references/explore.md` / `references/edit.md`.
- Verifying actual runtime thread/lock violations rather than static heuristic candidates -> `references/debugger.md`.
