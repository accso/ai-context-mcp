# Repository Agent Instructions

## AI Context

This repository uses local AI context MCP servers — one per project below.
Each server provides indexed code context (module maps, ranked search, file content)
scoped exclusively to its project. Use the single repo-wide skill
`ai-context-workflow` to route queries to the right project server.

| Skill | MCP server | When to use |
|-------|------------|-------------|
| `ai-context-workflow` | any registered *-context server | Code context / architecture across the repo |
| `ai-context-context-workflow` (deprecated) | `ai-context-context` | Old per-project skill; prefer `ai-context-workflow` |

