# Build, Run, Test & VCS

Use the IDE's own execution and VCS integration rather than assuming shell equivalents exist or behave the same way — the MCP server exposes them precisely so state stays consistent with what's shown in the IDE.

## Toolsets covered
- **Execution tools** (part of Analysis/Execution) - discover and run run-configurations or ad-hoc code locations (tests, `main`, etc.), with or without waiting for exit; get build errors via `build_project`.
- **Terminal toolset** - execute shell commands through the IDE's integrated terminal.
- **VCS toolset** - inspect version-control status via `git_status`, consistent with the IDE's VCS view.
- **Run Notebook toolset** - execute notebook cells/files (e.g. Jupyter) and retrieve their output.
- **Inspection Generator MCP toolset** - generate a custom IntelliJ inspection from a description.
- **Inspection KTS MCP toolset** - author/run Kotlin-script-based (`.kts`) custom inspections.

## Activation Gate (tool availability)
Use this guidance only when at least one of these is available in the current MCP session:
- `get_run_configurations` + `execute_run_configuration` (execution), or
- `execute_terminal_command` (terminal), or
- `git_status` (VCS)

If none of these are available, state the blocker explicitly. Do not silently substitute a raw shell tool for an IDE-integrated action (e.g. don't call plain shell `git status` instead of `git_status`) — the IDE-integrated tools keep state consistent with what's shown in the IDE UI; only fall back to raw shell after saying so.

Tool names below reflect this session's live MCP tool schema (bare names, no `idea_` prefix, invoked via the `idea-mcp-execute_tool` dispatcher) — confirmed live 2026-08-13. This session's schema has no separate `find_commit_by_message` tool; verify exact names/parameters against the live MCP tool schema before calling.

## Tool reference

### Execution
| Situation | Tool | Key parameters |
|---|---|---|
| List available run configurations | `get_run_configurations` | `projectPath?` |
| Find runnable entry points in a file (mains, tests) | `get_run_configurations` | `filePath` (set to discover entry points instead of listing all configs), `projectPath?` |
| Execute a run configuration | `execute_run_configuration` | `configurationName` (resolve from `get_run_configurations`, never invent it) or `filePath`+`line`; `waitForExit?`; `programArguments?`/`workingDirectory?`/`envs?` only if `supportsDynamicLaunchOverrides` is true for that configuration |
| Build the project or specific files, get compiler errors | `build_project` | `filesToRebuild?` (omit for full build), `projectPath?` |

### Terminal
| Situation | Tool | Key parameters |
|---|---|---|
| Run a shell command through the IDE's integrated terminal | `execute_terminal_command` | `command` (required), `workingDirectory?`, `timeout?` (raise explicitly for long-running commands and say so) |

### VCS
| Situation | Tool | Key parameters |
|---|---|---|
| Get working-tree status (modified/added/deleted files) | `git_status` | `projectPath?` |

### Custom inspections (inspection.kts)
| Situation | Tool | Key parameters |
|---|---|---|
| Get the available scripting API for a language | `generate_inspection_kts_api` | `language` (required) |
| See example inspection templates | `generate_inspection_kts_examples` | `language?` |
| Inspect the PSI tree for a code snippet | `generate_psi_tree` | `code` (required), `language` (required) |
| Run an inspection script against a target file | `run_inspection_kts` | `script` (required), `filePath` (required target) |
| Validate an inspection script against spec examples | `validate_inspection_kts` | `script` (required) |

## Workflow
1. **Discover before running.** Use `get_run_configurations` to find valid configuration names or runnable `filePath`+`line` targets rather than guessing a configuration name.
2. **Non-debug by default.** Use `execute_run_configuration` (no debugger) unless the user needs to inspect runtime state — that's what `references/debugger.md` is for.
3. **Validate after execution.** After a build/run, report exit code and surface errors/failures verbatim (with file/line when available) instead of summarizing away detail.
4. **Terminal is a fallback, not a first choice.** Prefer a dedicated tool (execution, VCS, build) when one exists; use `execute_terminal_command` for anything the specialized toolsets don't cover (custom scripts, package managers, one-off shell commands).
5. **VCS before large edits.** Check `git_status` before and after a batch of changes so you can report exactly what changed, and to avoid clobbering uncommitted work.
6. **Inspections are code, not one-off answers.** When asked to enforce a project-specific rule repeatedly, generate a reusable inspection (`generate_inspection_kts_api` → `run_inspection_kts` → `validate_inspection_kts`) rather than manually flagging violations each time.

## Argument hygiene
- Pass `projectPath` when known to avoid ambiguity across multiple open projects.
- Resolve `configurationName` only from `get_run_configurations` output; never pass a test method name or other derived identifier as if it were a configuration name.
- Do not pass launch overrides (`programArguments`, `workingDirectory`, `envs`) unless `supportsDynamicLaunchOverrides` is true for the chosen run configuration — check that flag first.
- Timeouts matter: long-running builds/tests may need an explicit higher `timeout`; state assumptions if you raise one.

## When NOT to use this guidance
- Investigating code without running anything -> `references/explore.md`.
- Editing/refactoring source -> `references/edit.md`.
- Inspecting runtime values while paused -> `references/debugger.md`.
