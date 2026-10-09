---
apply: always
---

# Use the IntelliJ IDEA MCP server for codebase interaction

When the `idea` MCP server is connected and a project is open in the IDE, prefer its `idea_*` tools over generic file-system, shell, or grep-based approaches for interacting with this codebase. The IDE's live index understands code semantics (imports, references, code style); text tools don't.

Load the **`idea-mcp`** skill before doing any non-trivial work in this codebase — it routes to the specific reference (explore / edit / ops / plugin-dev / debugger) for the task at hand and lists the exact tool names and parameters to use. Do not guess or paraphrase tool names; follow what the skill and the live MCP tool schema say.

Only fall back to plain file/shell operations when no suitable `idea_*` tool exists for the task, or when the `idea` MCP server is unavailable — and say so explicitly rather than silently switching.
