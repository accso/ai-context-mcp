# Explore & Understand Code

Use the IDE's own index (not shell grep) to answer "where/what/why" questions about a project. This is the read-only reconnaissance phase that should precede edits.

## Toolsets covered
- **Read toolset** - read file contents/structure through the IDE (handles decompiled sources, library/JAR/JRT entries too).
- **Search toolset** - full-text/regex search across the project, find files by name/glob, find usages, find symbols.
- **Code Insight toolset** - symbol info (declaration, signature, docs) at a file/line/column, equivalent to Quick Documentation.
- **Analysis toolset** - project-wide build, file-level problems/inspections, dependencies, and module listing.

## Activation Gate (tool availability)
Use this guidance only when at least the following tools are available in the current MCP session:
- one read tool: `idea_read_file` or `idea_get_file_text_by_path`
- one search tool: `idea_search_symbol`, `idea_search_text`, or `idea_search_in_files_by_text`
- `idea_get_file_problems` (for the "check health" step)

If none of these are available, state the blocker explicitly and fall back to shell-based reading/grep instead of assuming IDE-index tools exist.

Tool names and parameter names below are current as of the JetBrains MCP Server 2025.2+ documentation, but the live MCP tool schema in your session is the source of truth — if a tool listed here isn't present, or its parameters differ, follow the session's schema instead of this table.

## Tool reference

### Read
| Situation | Tool | Key parameters |
|---|---|---|
| Read a file with numbered lines (handles `.jar`/decompiled `.class` too) | `idea_read_file` | `filePath` (required), `projectPath?`, optional line-range window if supported |
| Quick read by project-relative path | `idea_get_file_text_by_path` | `pathInProject` (required), `projectPath?` |
| Browse a directory tree | `idea_list_directory_tree` | `pathInProject?` (defaults to project root), `projectPath?`, `maxDepth?` |
| See which files are open in the editor | `idea_get_all_open_file_paths` | `projectPath?` |
| Open a file in the editor | `idea_open_file_in_editor` | `filePath` (required), `projectPath?` |

Always prefer `idea_read_file` over shell `cat` — it decompiles class files and reads jar entries; manual archive extraction cannot do this and breaks IDE source resolution.

### Search
| Situation | Tool | Key parameters |
|---|---|---|
| Find files by partial name | `idea_find_files_by_name_keyword` | `nameKeyword` (required), `projectPath?` |
| Find files by glob pattern | `idea_search_file` / `idea_find_files_by_glob` | `globPattern` (required), `projectPath?` |
| Literal text search with line/column coordinates | `idea_search_text` | `query` (required), `filePath?` (scope to one file), `projectPath?` |
| Regex search with coordinates | `idea_search_regex` | `pattern` (required), `filePath?`, `projectPath?` |
| Large-scale project-wide text/regex search | `idea_search_in_files_by_text` / `idea_search_in_files_by_regex` | `query`/`pattern` (required), `scopeGlob?`, `projectPath?` |
| Semantic symbol search (class/method/field name fragments) | `idea_search_symbol` | `query` (required), `includeExternal?` (set `true` to include SDK/library symbols), `projectPath?` |
| Resolve the symbol at a specific file:line:col | `idea_get_symbol_info` | `filePath`, `line`, `column` (all required, 1-based), `projectPath?` |

For call-graph or usage questions: use `idea_search_symbol` to locate the symbol first, then proceed with further targeted reads. Prefer the narrowest search (single file, specific glob, or scoped symbol name) before falling back to a project-wide search, to keep results reviewable.

### Analysis
| Situation | Tool | Key parameters |
|---|---|---|
| Find compile errors/warnings in a file | `idea_get_file_problems` | `filePath` (required), `projectPath?` |
| Build the project or specific files | `idea_build_project` | `filesToRebuild?` (omit for a full build), `projectPath?` |
| List all modules | `idea_get_project_modules` | `projectPath?` |
| List all dependencies | `idea_get_project_dependencies` | `projectPath?` |

After analysis/build tools report problems, quote file + line + message back to the user rather than paraphrasing vaguely.

## Workflow
1. **Orient first.** If you don't know the project layout, list modules/dependencies before searching blindly.
2. **Search over guess.** Prefer the search toolset (symbol search, text/regex search, glob file search) over asking the user or grepping via shell - the IDE index is authoritative and includes indexed metadata shell tools don't see.
3. **Resolve exact locations before acting.** Use search/read results to get exact `filePath` + 1-based `line`/`column` before calling any tool that requires a code position (e.g. code insight, later refactoring/debugging).
4. **Check health before editing.** Before modifying a file, check its current errors/warnings so you can distinguish pre-existing issues from ones you introduce.
5. **Library code is readable too.** The read toolset can decompile `.class` files inside JARs/JRT and read bundled SDK sources - never try to unpack archives manually with shell commands; ask the read tool for the path directly.
6. **Pass `projectPath`** on every call when it is known, to avoid ambiguous resolution in multi-project sessions.

## Argument hygiene
- `line`/`column` are 1-based; never invent them — always source them from a prior read/search result in the same conversation.
- Treat `filePath` values as opaque strings copied from a previous tool result, not hand-constructed paths, especially for library/JAR/decompiled sources.
- Omit optional parameters rather than passing placeholder values (`""`, `"."`, fake paths).

## When NOT to use this guidance
- If you already have exact runtime values/branch behavior in question, use `references/debugger.md` instead - static exploration cannot prove runtime behavior.
- If the task is only to make a code change and you already know the exact location, go straight to the file/formatting/refactoring toolsets (see `references/edit.md`).

## Practical notes
- Tool names and parameters exposed by the MCP server can vary slightly by IDE version. Before calling a tool, check its live description/schema exposed by the MCP session in the current client - call it as described there rather than assuming a name from memory.
