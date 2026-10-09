# AI Context

Token-efficient, **locally run** context layer for AI coding agents. It indexes your repository (code chunks, symbols, call/import graph, docs) and serves exactly the context relevant to a query over MCP — so the agent doesn't have to dump whole files into its context window. Indexing and retrieval run entirely on your machine; see [Privacy boundary](#privacy-boundary) for what that does and does not guarantee.

Works with any MCP client that speaks StreamableHTTP, e.g. GitHub Copilot (VS Code and IntelliJ plugins), JetBrains AI Assistant setups, Crush, and other MCP-capable agents.

## Why

- **Minimal tokens, maximum relevance** — the agent drives its own retrieval loop and reads only what it needs. No LLM sits in the retrieval path.
- **Always current** — a file watcher (2 s debounce) plus SHA-delta reconciliation on start keeps the index fresh and self-healing.
- **Precise** — tree-sitter symbol index with an identifier fast-path, hybrid vector search with ColBERT re-ranking, and a call/import graph for blast-radius analysis.
- **Local by design** — embeddings, re-ranking, vector search and assembly all run locally. What the connected agent does with the returned context is outside this tool's control (see [Privacy boundary](#privacy-boundary)).

## How it works

```
Agent (MCP client)
   │  MCP over StreamableHTTP
   ▼
MCP Server ── repo_structure · semantic_search · get_file · analyze_impact · resolve_symbol
   │
   ▼
Shared infrastructure: Qdrant (vectors) · PostgreSQL (index, maps, symbols, graph) · Inference service (embeddings + re-rank)
   ▲
   │  delta indexing on save
Watcher / Indexer (Python)
```

### MCP tools

| Tool | Purpose |
|------|---------|
| `repo_structure` | Project map and directory tree — call first for orientation |
| `semantic_search` | Symbol fast-path, else hybrid/ColBERT vector search with ranked hits |
| `get_file` | Exact line-range slices from the index (never reads from disk) |
| `analyze_impact` | Callers/callees and likely-covering tests (blast radius) |
| `resolve_symbol` | Definition/references/hover via a real language server |

Operator-only tools (`list_recent_changes`, `get_index_status`) live on `/mcp-admin`.

## Privacy boundary

Indexing, embeddings, re-ranking, vector search and metadata storage run entirely on your machine, and this tooling sends nothing to external servers. It has no control over what the connected agent does with the results it requests: with a cloud-hosted model, returned code excerpts become part of the agent's prompt and are sent to that provider.

- **Local (this tool):** file indexing, embeddings, re-ranking, Qdrant, PostgreSQL, the MCP server. L1 file summaries use the LLM provider you configure (`lmstudio`, `omlx`, `ollama`) and stay local only as long as its base URL points to your machine.
- **Outside this tool's control:** the agent/IDE plugin calling the MCP tools (e.g. GitHub Copilot, JetBrains AI Assistant) and the model behind it. If that model is hosted by a third party, any context the agent requests may be transmitted to it.
- **If data must not leave your environment,** use an agent backed by a local or otherwise approved model, and review the provider terms of the agent you connect.

## Repository layout

| Path | Contents |
|------|----------|
| `.ai-context/` | The system itself: `mcp-server/` (TypeScript), `watcher/` (Python indexer), `inference/`, `infrastructure/` (Docker Compose for Qdrant/Postgres), `skills/`, `scripts/`, `tests/`, `docs/`, `config.yaml` |
| `scripts/` | Wrapper scripts behind the `mise` tasks |
| `mise.toml` | Tool versions (Java, Node, pnpm, Python 3.12) and tasks |
| `AGENTS.md` | Instructions for coding agents working in this repo |

## Getting started

Prerequisites: Docker (or Rancher Desktop), [mise](https://mise.jdx.dev), `jq`, `python3`.

```bash
brew install jq python3
pip3 install -r .ai-context/scripts/requirements.txt
```

Configure your projects in [`.ai-context/config.yaml`](.ai-context/config.yaml), and the local LLM provider used for L1 file summaries (`lmstudio`, `omlx` or `ollama`). Embeddings and re-ranking need no setup.

Then start the system (two terminals, or use the `-daemon` variants):

```bash
mise run ai-context-infra-up   # shared Qdrant + PostgreSQL + inference service
mise run ai-context-up         # validate config, initial index, MCP server + watcher
```

On first use, MCP entries are written to the configured IDE config files (e.g. `.vscode/mcp.json`). Restart your IDE once to pick up the server.

### Mise tasks

| Task | Description |
|------|-------------|
| `ai-context-infra-up` / `-daemon` | Start shared infrastructure |
| `ai-context-infra-down` | Stop shared infrastructure |
| `ai-context-up` / `-daemon` | Start the AI context layer |
| `ai-context-down` | Stop the AI context layer |
| `ai-context-backup` | Back up the database |
| `ai-context-reset` | Full wipe and re-init |
| `ai-context-clean` | Clean up resources |

## Testing

Three tiers, picked by blast radius:

```bash
.ai-context/scripts/test.sh unit          # seconds, no Docker
.ai-context/scripts/test.sh integration   # disposable Postgres/Qdrant
.ai-context/scripts/test.sh e2e           # full real pipeline
```

See [`docs/testing.md`](.ai-context/docs/testing.md).

## Documentation

Full entry point: [`.ai-context/README.md`](.ai-context/README.md). Deeper references in [`.ai-context/docs/`](.ai-context/docs/):

- [Architecture deep-dive](.ai-context/docs/architecture-deep-dive.md)
- [Concepts and prior art](.ai-context/docs/concepts-and-prior-art.md)
- [Configuration](.ai-context/docs/configuration.md)
- [IDE integration](.ai-context/docs/ide-integration.md)
- [LSP integration](.ai-context/docs/lsp-integration.md)
- [Backups](.ai-context/docs/backups.md)
- [Testing](.ai-context/docs/testing.md)
- [Troubleshooting](.ai-context/docs/troubleshooting.md)
- [Benchmark results](.ai-context/docs/benchmark/benchmark-results.md)

## Author

Created and maintained by **Florestan Sulimma** at **Accso GmbH**.

## License

Apache License 2.0, Copyright 2026 Accso GmbH — see [LICENSE](LICENSE), [NOTICE](NOTICE) and [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
