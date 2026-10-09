# Build, Run, Test & VCS

Use the IDE's own execution and VCS integration rather than assuming shell equivalents exist or behave the same way — the MCP server exposes them precisely so state stays consistent with what's shown in the IDE.

## Toolsets covered
- **Execution tools** (part of Analysis/Execution) - discover and run run-configurations or ad-hoc code locations (tests, `main`, etc.), with or without waiting for exit; get build errors via `idea_build_project`.
- **Terminal toolset** - execute shell commands through the IDE's integrated terminal.
- **VCS toolset** - inspect version-control status, diffs, history, and perform VCS operations (e.g. commit-related actions) consistent with the IDE's VCS view.
- **Run Notebook toolset** - execute notebook cells/files (e.g. Jupyter) and retrieve their output.
- **Inspection Generator MCP toolset** - generate a custom IntelliJ inspection from a description.
- **Inspection KTS MCP toolset** - author/run Kotlin-script-based (`.kts`) custom inspections.

## Activation Gate (tool availability)
Use this guidance only when at least one of these is available in the current MCP session:
- `idea_get_run_configurations` + `idea_execute_run_configuration` (execution), or
- `idea_execute_terminal_command` (terminal), or
- `idea_get_project_vcs_status` (VCS)

If none of these are available, state the blocker explicitly. Do not silently substitute a raw shell tool for an IDE-integrated action (e.g. don't call plain shell `git status` instead of `idea_get_project_vcs_status`) — the IDE-integrated tools keep state consistent with what's shown in the IDE UI; only fall back to raw shell after saying so.

Run Notebook and DevKit-adjacent tool names were not independently confirmed against public JetBrains documentation at time of writing — verify their exact names/parameters against the live MCP tool schema in your session before calling.

## Tool reference

### Execution
| Situation | Tool | Key parameters |
|---|---|---|
| List available run configurations | `idea_get_run_configurations` | `projectPath?` |
| Find runnable entry points in a file (mains, tests) | `idea_get_run_configurations` | `filePath` (set to discover entry points instead of listing all configs), `projectPath?` |
| Execute a run configuration | `idea_execute_run_configuration` | `configurationName` (resolve from `idea_get_run_configurations`, never invent it) or `filePath`+`line`; `waitForExit?`; `programArguments?`/`workingDirectory?`/`envs?` only if `supportsDynamicLaunchOverrides` is true for that configuration |
| Build the project or specific files, get compiler errors | `idea_build_project` | `filesToRebuild?` (omit for full build), `projectPath?` |

### Terminal
| Situation | Tool | Key parameters |
|---|---|---|
| Run a shell command through the IDE's integrated terminal | `idea_execute_terminal_command` | `command` (required), `workingDirectory?`, `timeout?` (raise explicitly for long-running commands and say so) |

### VCS
| Situation | Tool | Key parameters |
|---|---|---|
| Get working-tree status (modified/added/deleted files) | `idea_get_project_vcs_status` | `projectPath?` |
| Find a commit by message | `idea_find_commit_by_message` | `query` (required), `projectPath?` |

### Custom inspections (inspection.kts)
| Situation | Tool | Key parameters |
|---|---|---|
| Get the available scripting API for a language | `idea_generate_inspection_kts_api` | `language` (required) |
| See example inspection templates | `idea_generate_inspection_kts_examples` | `language?` |
| Inspect the PSI tree for a code snippet | `idea_generate_psi_tree` | `code` (required), `language` (required) |
| Run an inspection script against a target file | `idea_run_inspection_kts` | `script` (required), `filePath` (required target) |
| Validate an inspection script against spec examples | `idea_validate_inspection_kts` | `script` (required) |

## Workflow
1. **Discover before running.** Use `idea_get_run_configurations` to find valid configuration names or runnable `filePath`+`line` targets rather than guessing a configuration name.
2. **Non-debug by default.** Use `idea_execute_run_configuration` (no debugger) unless the user needs to inspect runtime state — that's what `references/debugger.md` is for.
3. **Validate after execution.** After a build/run, report exit code and surface errors/failures verbatim (with file/line when available) instead of summarizing away detail.
4. **Terminal is a fallback, not a first choice.** Prefer a dedicated tool (execution, VCS, build) when one exists; use `idea_execute_terminal_command` for anything the specialized toolsets don't cover (custom scripts, package managers, one-off shell commands).
5. **VCS before large edits.** Check `idea_get_project_vcs_status` before and after a batch of changes so you can report exactly what changed, and to avoid clobbering uncommitted work.
6. **Inspections are code, not one-off answers.** When asked to enforce a project-specific rule repeatedly, generate a reusable inspection (`idea_generate_inspection_kts_api` → `idea_run_inspection_kts` → `idea_validate_inspection_kts`) rather than manually flagging violations each time.

## Argument hygiene
- Pass `projectPath` when known to avoid ambiguity across multiple open projects.
- Resolve `configurationName` only from `idea_get_run_configurations` output; never pass a test method name or other derived identifier as if it were a configuration name.
- Do not pass launch overrides (`programArguments`, `workingDirectory`, `envs`) unless `supportsDynamicLaunchOverrides` is true for the chosen run configuration — check that flag first.
- Timeouts matter: long-running builds/tests may need an explicit higher `timeout`; state assumptions if you raise one.

## When NOT to use this guidance
- Investigating code without running anything -> `references/explore.md`.
- Editing/refactoring source -> `references/edit.md`.
- Inspecting runtime values while paused -> `references/debugger.md`.
