# Modify Code Safely

Prefer IDE-native operations over manual text surgery whenever the MCP server exposes an equivalent tool — they respect the language model, update all references, and match the configured code style.

## Toolsets covered
- **File toolset** - create/read/write files and directories, apply targeted text edits, list directory trees.
- **Formatting toolset** - reformat a file or code range to the project's configured code style.
- **Refactoring toolset** - rename, move, safe-delete, extract method/variable, change signature, and other structural refactorings that update all call sites.

## Activation Gate (tool availability)
Use this guidance only when at least one editing tool is available in the current MCP session:
- `apply_patch` (text edits), and/or
- `rename_refactoring` (refactoring)

If none of these are available, state the blocker explicitly rather than falling back silently to raw shell file edits on an IDE-indexed project — shell edits bypass the language model and can desync the IDE index.

Tool names below reflect this session's live MCP tool schema (bare names, no `idea_` prefix, invoked via the `idea-mcp-execute_tool` dispatcher) — confirmed live 2026-08-13. This session's schema has no separate `replace_text_in_file` tool; treat the live schema as the source of truth if it differs.

## Tool reference

| Situation | Tool | Key parameters |
|---|---|---|
| Apply a unified diff or apply_patch-format patch | `apply_patch` | `filePath` or `file_path` (both accepted in testing — likely redundant since the patch body's own `*** Update/Add File: <path>` header carries the real target; harmless to include either), `patch` (required, unified-diff or apply_patch format) |
| Rename a symbol project-wide (refactor-safe) | `rename_refactoring` | `filePath`, `line`, `column` (all required, 1-based, identify the symbol), `newName` (required) |
| Create a new file | `create_new_file` | `filePath` (required, project-relative), `content?` |
| Reformat a file to the project's code style | `reformat_file` | `filePath` (required), `projectPath?` |
| List a directory tree (to place new files correctly) | `list_directory_tree` | `pathInProject?`, `projectPath?` |

**Never** hand-edit identifier renames via `apply_patch` — `rename_refactoring` updates all references, imports, and usages project-wide and understands the language; a patch-based text replace does not.

`rename_refactoring` and other structural refactorings (extract method/variable, move, safe-delete, change signature) identify their target by position (`filePath`+`line`+`column`), never by name alone — always resolve that position from a prior search/read result (see `references/explore.md`) before calling.

## Workflow
1. **Locate before editing.** Use `references/explore.md` first to get the exact file path and line/column, unless already known.
2. **Choose the right tool for the change:**
   - Renaming a symbol used elsewhere, changing a signature, extracting code -> use the **refactoring toolset**, not manual find/replace. It updates all references project-wide and understands the language.
   - Small localized text changes (new lines, edits within one file) -> use `apply_patch` rather than rewriting the whole file.
   - New files/directories -> use `create_new_file` so they land correctly in the module structure.
3. **Reformat after non-trivial edits.** After manual/generated edits that might not match project style, run `reformat_file` on the touched file(s) rather than trying to hand-format.
4. **Validate.** After edits, use `build_project` / `get_file_problems` (from `references/explore.md`) to confirm the change compiles and didn't introduce new warnings/errors.
5. **One responsibility per call.** Don't bundle unrelated renames/edits into a single refactoring call; apply them as separate, reviewable operations.

## Argument hygiene
- Paths are relative to the project root unless a tool explicitly documents otherwise; line/column numbers are 1-based.
- Never invent line numbers — always source them from a prior read/search result in the same conversation, and refresh them if the file changed since.
- Do not fabricate placeholder values for optional parameters; omit them instead.
- Before a rename/refactor call, re-verify the target position with a fresh read if any prior edit in the same turn may have shifted line numbers.

## When NOT to use this guidance
- Pure investigation with no intended change -> `references/explore.md`.
- Running commands, tests, builds, or VCS actions -> `references/ops.md`.
- Debugging runtime behavior -> `references/debugger.md`.
