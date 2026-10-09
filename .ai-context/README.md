# AI Context — Project Setup

Token-efficient, locally run AI context layer for coding agents — e.g. GitHub Copilot (native VS Code plugin, or the IntelliJ plugin backing JetBrains AI Assistant), Crush (a distinct successor to the archived `opencode-ai/opencode`), and `sst/opencode` — a *separate*, actively maintained terminal agent that happens to share the "OpenCode" name with Crush's predecessor. These are not the same project (see note below); `sst/opencode` additionally supports a native JS/TS plugin API (custom tools, lifecycle hooks, built-in LSP) beyond plain MCP consumption, which the rest of this doc treats as "any MCP client" unless called out.

---

## The Problem We Are Solving

AI coding assistants are most useful when they understand the codebase they are working in. The naive approach — dump as many source files as possible into the context window — runs into hard limits fast:

- **Token budgets are finite.** Even large-context models have a practical ceiling. Stuffing a whole repo in means either truncation or an enormous cost per request.
- **More tokens ≠ better answers.** Irrelevant code is noise. The model has to work against it, not with it.
- **Full-project re-reads on every query are slow.** Large file dumps add measurable latency to every assistant interaction.
- **Private code must stay private.** Sending proprietary source to a hosted embedding or LLM API is often not acceptable in an enterprise context. (Our own indexing and retrieval never do this; see [Privacy boundary](#privacy-boundary) for what the connected agent can still do.)

What we need is a system that gives the assistant *exactly* the context that is relevant to the current query — no more, no less — and that does so locally: our own components send no data to external services.

---

## Privacy boundary

Indexing, embeddings, re-ranking, vector search and metadata storage run entirely on your machine, and this tooling sends nothing to external servers. It has no control over what the connected agent does with the results it requests: with a cloud-hosted model, returned code excerpts become part of the agent's prompt and are sent to that provider.

- **Local (this tool):** file watcher, chunking, embeddings and re-ranking (inference service), Qdrant, PostgreSQL, the MCP server, the LSP bridge. L1 summaries use your configured LLM provider (`lmstudio_base_url` / `omlx_base_url` / Ollama) and are only local if that URL points to your machine.
- **Outside this tool's control:** the agent or IDE plugin that calls `repo_structure`, `semantic_search`, `get_file`, `analyze_impact` or `resolve_symbol`, and the model behind it. If that model is hosted by a third party, requested code, file content, summaries and query results can be transmitted to it, and that provider may no longer count as "local" in your compliance context.
- **If data must not leave your environment,** connect only agents backed by a local or approved model and check the provider's data-handling terms. Restricting which projects are indexed also limits what an agent can request.

## Concepts

This project sits at the intersection of two research threads usually
discussed separately — agent-driven iterative retrieval, and
repository-level code intelligence — plus a set of reference
implementations whose patterns we adopted directly. See
**[`docs/concepts-and-prior-art.md`](docs/concepts-and-prior-art.md)** for
the full paper list and the 6 reference repos we drew specific patterns
from (byterover-cli, rlm, OpenViking, mcp-local-rag, mcp-rag-server, PageIndex).
These projects are **inspiration only** — none of them is a dependency or
is run by this system; we reimplemented selected ideas in our own code.

---

## What We Achieve

- **Minimal tokens, maximum relevance.** The calling agent drives its own retrieval loop (`repo_structure` → `semantic_search` → `get_file`) and reads only the ranked results and files it decides it needs — no LLM-produced summary sits between the agent and the index to pay for or to hallucinate through.
- **Real-time accuracy.** A file watcher with a 2-second debounce keeps the index current while you type (see [File watching reliability](docs/architecture-deep-dive.md#file-watching-reliability--native-events-vs-the-polling-fallback) — this depends on your container runtime correctly forwarding native file events; a polling fallback covers the setups where it doesn't). On every start the watcher runs a SHA-delta reconciliation to catch any files changed while it was stopped (e.g. after a `git pull`), and self-heals any internal Qdrant/DB inconsistencies automatically.
- **Local indexing and retrieval.** All embedding, summarisation, vector search, and context assembly run on your own machine; none of our components send source code to an external service. What the *calling agent* does with the context it requests is outside our control — see [Privacy boundary](#privacy-boundary).
- **Precise symbol lookup.** Classes, functions, interfaces and methods are extracted via tree-sitter AST parsing (with a chunker-derived fallback for languages without a tree-sitter grammar here, and for switch/when/match dispatch arms in any language) into a `symbol_index` table. `semantic_search` resolves exact identifiers directly via this table before falling back to vector search — no full semantic search needed for a known name.
- **Cross-project awareness (agent-driven, not automatic).** A shared library used by an application can be declared as a `sources` entry in config. This doesn't trigger an automatic cross-collection search today — `assembleContext()` (the function that would do that) exists in `context_assembler.ts` but isn't wired to any live route. In practice, cross-project awareness surfaces as guidance in the generated skill file/instructions telling the agent to repeat the `repo_structure`/`semantic_search` workflow against the source project's own MCP server when the answer likely lives there.
- **Agent-agnostic delivery.** Any coding agent that speaks MCP over StreamableHTTP can consume the context without integration work — e.g. GitHub Copilot's native VS Code plugin, GitHub Copilot's IntelliJ plugin (the backend JetBrains AI Assistant setups use), or Crush (the terminal agent that succeeded the archived `opencode-ai/opencode` — not to be confused with the separate, actively maintained `sst/opencode` project of the same name; see the naming note above).
- **Reasoning-based documentation navigation.** For long-form documentation files (ADRs, wikis, architecture docs, RFCs), the system builds a hierarchical tree index (an idea inspired by [PageIndex](docs/concepts-and-prior-art.md#pageindex), which we do not use — the implementation is our own); `get_file` auto-routes to it — delivering section-precise, context-aware documentation context instead of chunk-level similarity hits.
- **Exact line-range retrieval with no disk reads.** `get_file` slices the requested `start_line`/`end_line` from chunk payloads already stored in Qdrant, or from `body_text` stored on doc-tree nodes in PostgreSQL. The project file is never read from disk during an MCP request.

---

## Architecture

The system is split into three layers that run independently and communicate through the shared infrastructure (DB + inference service). There is no LLM anywhere in the retrieval path — the calling agent is the only reasoning entity; see [Architecture Deep-Dive](docs/architecture-deep-dive.md#earlier-design-routing-llm-removed) for the design this replaced and why.

```
Agent  (any MCP client — e.g. GitHub Copilot, JetBrains AI Assistant, Crush)
        │
        │  MCP over StreamableHTTP — agent drives the loop itself
        ▼
 ┌───────────────────────────────────────────────────────────┐
 │  MCP Server  (/mcp — 5 tools, agent-facing)               │
 │                                                           │
 │  repo_structure([dir_path], [depth])                      │
 │    → L0 map + directory tree                (PostgreSQL)  │
 │  semantic_search(query)                                   │
 │    → identifier fast-path → symbol_index    (PostgreSQL)  │
 │    → else hybrid/ColBERT vector search        (Qdrant)    │
 │    ← raw ranked hits + L1 summaries (no LLM, no prose)    │
 │  get_file(file_path, [start_line], [end_line])            │
 │    → exact line-range slices from stored chunk text       │
 │      (Qdrant) or doc-tree node body_text (PostgreSQL);    │
 │      never reads the file from disk                       │
 │  analyze_impact(symbol_name, direction, [depth], …)       │
 │    → BFS over call_edges/import_edges (PostgreSQL)        │
 │    ← callers/callees, ambiguous targets, test files       │
 │  resolve_symbol(file_path, line, character, kind)         │
 │    → real LSP definition/references/hover via LSP Bridge  │
 │      (auto-detected per project/language — lsp.enabled_   │
 │      languages is never set by hand, see below)           │
 │                                                           │
 │  /mcp-admin → list_recent_changes, get_index_status       │
 │    (operator-only — not part of the agent-facing set)     │
 └───────────────────────────────────────────────────────────┘
        │
        ▼
 ┌─────────────────────────────────────────────────────────────┐
 │  Shared Infrastructure  (.ai-context/infrastructure/)       │
 │  (always-on, one shared instance per machine, per project)  │
 │                                                             │
 │  Qdrant       L2 code chunks (per-project collections)      │
 │  PostgreSQL   file index, L0 map, L1 summaries,             │
 │               doc_trees, symbol_index                       │
 │  Inference    embeddings (nomic-embed-text-v1.5, ~100 ms)   │
 │  Service      + ColBERT re-ranking (MaxSim, ~100–300 ms/20) │
 │               POST /embeddings · POST /rerank               │
 │               FastEmbed/onnxruntime — no torch, no LM       │
 │               Studio/omlx dependency for either             │
 └─────────────────────────────────────────────────────────────┘
        ▲
        │  embed chunks + delta index on save (2 s debounce)
        │  stable content-hash chunk IDs → only changed chunks re-embedded
 ┌────────────────────────────────────────────────────────┐
 │  Indexer / Watcher  (Python, Docker)                   │
 │   ├── Stable chunk IDs  (UUID v5 content hashes)       │
 │   │     Only changed/new chunks re-embedded            │
 │   │     Unchanged chunks reused without re-embedding   │
 │   ├── AST/Symbol index (tree-sitter + chunker fallback)│
 │   │     Extracts classes, functions, interfaces, …     │
 │   │     Stored in symbol_index PostgreSQL table        │
 │   │     Used by semantic_search's identifier fast-path │
 │   ├── [all existing features: watcher, chunker, …]     │
 └────────────────────────────────────────────────────────┘
```

A deep technical companion doc covers everything below in full: how queries flow through the three retrieval layers (L0 map / L1 summaries / L2 chunks), the MCP manifest design decisions, re-ranking (ColBERT + keyword fallback), query expansion, the symbol index, the call/import graph and `analyze_impact`, structure-aware chunking per file type, and file-watcher reliability (including the native-events-vs-polling-fallback caveat on macOS container runtimes) — see **[Architecture Deep-Dive](docs/architecture-deep-dive.md)**.

For the visual/interactive version of the same material, see [`docs/interactive/overview.html`](docs/interactive/overview.html) (retrieval layers, query flow), [`docs/interactive/chunking.html`](docs/interactive/chunking.html) (chunking strategies), and [`docs/interactive/context-map.html`](docs/interactive/context-map.html) / [`docs/interactive/infrastructure.html`](docs/interactive/infrastructure.html) (re-ranking, symbol index, call graph, watcher topology).

---

## Getting Started

### 1. Install prerequisites

Be sure that Rancher Desktop or Docker is installed.

```bash
brew install jq python3
pip3 install -r .ai-context/scripts/requirements.txt
```

**Environment variables** (optional for most setups):  
Copy `.ai-context/.env.example` to `.ai-context/.env` and fill in any values needed:

```bash
cp .ai-context/.env.example .ai-context/.env
```

| Variable | When needed |
|----------|-------------|
| `OMLX_API_KEY` | Only if you have authentication enabled on your local omlx instance (default: none needed) |
| `OLLAMA_URL` | Only to override the default Ollama URL (`http://localhost:11434`) |

Most local setups (lmstudio / omlx without auth) need no env vars at all.

### 2. Configure your LLM provider (for L1 file summaries)

Embeddings and re-ranking need no setup at all — they're generated by the shared, mandatory **inference service** (`.ai-context/inference/`, FastEmbed/onnxruntime), which `mise run ai-context-infra-up` builds and starts automatically alongside Qdrant/PostgreSQL. There is nothing to install, load, or configure for either.

The only thing you still need to set up locally is the **LLM** used for L1 file summaries. This setup supports **three local inference providers** for it: `lmstudio` (port 1234), `omlx` (port 8000), and `ollama`. Switch between them via `llm.provider` in `config.yaml`.

**For L1 Summary Generation:**

The L1 task (4–6 sentence technical summary, ≤ 250 tokens, `temperature: 0.1`) is structured output — any modern code model from ~3 B parameters upward produces accurate results. There is no routing LLM anymore (see [Architecture Deep-Dive](docs/architecture-deep-dive.md#earlier-design-routing-llm-removed)), so this model is sized purely for L1 summary quality — no same-model constraint with another local-inference consumer.

| Model | RAM | MLX | Reasoning | Notes |
|---|---|---|---|---|
| `Qwen3-Coder-30B-A3B-Instruct-MLX-4bit` | ~18 GB stored / ~3 B active (MoE) | ✅ | ❌ | **Recommended** — best code quality, non-reasoning |
| `mistralai_devstral-small-2-24b-instruct-2512-mlx` | ~16 GB | ✅ | ❌ | Best quality — code-specialized, SWE-bench optimized |
| `gemma-4-12B-it-MLX-8bit` | ~9 GB | ✅ | ❌ | Reliable tool calling, lighter-weight alternative |
| `google/gemma-3-12b` | ~10.7 GB | ✅ | ❌ | Good quality, borderline 10 GB limit |
| `mlx-community/gemma-4-12b-it-4bit` | ~5 GB | ✅ | ❌ | Memory-tight fallback |
| `lmstudio-community/GLM-4.7-Flash-MLX-4bit` | ~16 GB stored / 3 B active | ✅ | ❌ | MoE model, coding + tool-use, non-reasoning |
| `phi-4`, `granite-4`, `ministral-3` | varies | ❌ | ❌ | GGUF only — no MLX builds available |

> Models with reasoning (`deepseek-r1`, `qwen3-thinking`) add 30–60 s overhead per file even with `disable_thinking: true`, because LM Studio's logging shows the thinking phase still initialises server-side. Use a native non-reasoning model to avoid this entirely.

**Thinking models** (`qwen3-4b-thinking`, `deepseek-r1-*`, etc.) emit `<think>…</think>` chain-of-thought blocks by default. The summariser strips these automatically. Set `llm.disable_thinking: true` in `config.yaml` to also suppress the thinking phase at the API level — this saves 30–60 s per file on the initial index.

Steps:
1. Open LM Studio → search and download your chosen model (MLX variant for Apple Silicon)
2. Load the model and enable the Local Server on port `1234`
3. Set `llm.model` in `config.yaml` to the model ID shown in LM Studio
4. If using a thinking model: set `llm.disable_thinking: true`

**If using omlx instead of LM Studio** (recommended for Apple Silicon with MLX models):
1. Install and start omlx on port `8000`
2. Load your chosen MLX model in omlx
3. Set `llm.provider: "omlx"` in `config.yaml`
4. Set `llm.model` to match the omlx model ID
5. If your omlx instance has authentication enabled, set `OMLX_API_KEY` in `.ai-context/.env`

Once your inference provider is running, proceed to [Running the System](#running-the-system).

---

## Project Configuration

All projects are defined in **`.ai-context/config.yaml`**. Each entry in the `projects` list is one independently indexed and served context layer with its own Qdrant collection and MCP server.

### Key fields

| Field | Required | Description |
|---|---|---|
| `name` | ✅ | Kebab-case unique key — becomes the Qdrant collection, DB key, and `mcp.json` server key |
| `display_name` | ✅ | Human-readable label shown in tool descriptions |
| `profile` | — (auto) | Structured technology-mix object, **auto-detected** from the project's files — not a config field; setting it in `config.yaml` is a validation error. See [Project Configuration — Full Reference](docs/configuration.md#profile-auto-detection) for the full schema. |
| `lsp.idle_timeout_minutes` | — | Optional per-project override (default **15**, `scripts/validate_config.py:DEFAULT_LSP_IDLE_TIMEOUT_MINUTES`) for how long an idle per-`(project, language)` language-server process is kept warm in the LSP Bridge pool before eviction. A performance knob only — never a correctness choice, since `lsp.enabled_languages` itself is always auto-detected (see `resolve_symbol` above), never hand-set. |
| `root_path` | ✅ | Path to the project root, relative to the repo root. Use `""` for the whole repo |
| `mcp.port` | ✅ | Port for this project's MCP server. **Must be set manually** — pick a free port unique per machine |
| `mcp.max_context_tokens` | — | Token budget for `assembleContext()`'s L0+L1+L2 assembly (default `8000`) — **not currently consumed by any live route**; `semantic_search` and `/search` both use `rankedSemanticSearch`, which ignores this field. Reserved for if/when `assembleContext()` is wired back to a route. |

> **Project-scope boundary — files outside `root_path` are never indexed or
> answerable, even if they are operationally related.** Every retrieval
> path (`semantic_search`, `get_file`, `repo_structure`, and the CLI
> `/search`) is scoped strictly to a project's configured `root_path` — the
> watcher container only ever has that directory mounted (see "Eligibility
> filter" below), so nothing outside it is visible to that project's MCP
> server at all. This is a deliberate isolation boundary, not a bug, and
> there is currently no cross-boundary fallback (`sources` federation is
> the supported way to pull in another *project's* context — see
> "Cross-project sources" below — but it still only ever reads from another
> project's own `root_path`, never from arbitrary paths outside any
> registered project).
>
> A common way to be surprised by this: this repository's own
> `ai-context` project (self-hosted, `root_path: ".ai-context"`) cannot
> answer questions about `scripts/ai-context-reset.sh` — that script lives
> one directory *above* `.ai-context/`, at the actual repo root, so it's
> outside this project's `root_path` and is never indexed. Anything at or
> above a project's configured root, however operationally close, needs
> either a project registered with a `root_path` that covers it, or to be
> asked about via a different tool/means entirely.

### Minimal single-project example

```yaml
shared:
  llm:
    provider: "omlx"                                   # or "lmstudio"
    model: "Qwen3-Coder-30B-A3B-Instruct-MLX-4bit"   # drives L1 summaries

projects:
  - name: "my-service"
    display_name: "My Service"
    root_path: "services/my-service"          # or "" for the entire repo
    mcp:
      port: 7100
```

Note: `profile` is auto-detected — see the table above. It is not set here. There is no `embedding:` block — embeddings are generated by the shared, mandatory inference service (see [Getting Started](#2-configure-your-llm-provider-for-l1-file-summaries)), not something you configure per project.

### Multi-project monorepo & cross-project sources

Full field reference, monorepo example, and cross-project `sources` federation config: **[Project Configuration — Full Reference](docs/configuration.md)**.

---

## Running the System

The system needs **two persistent terminals** — one for the shared infrastructure, one for the AI context layer. Both stay open while you work. If you'd rather not dedicate two terminals to this, use the `-daemon` variant of each command to start everything the same way and return control to your shell immediately instead — the underlying Docker containers are already daemonized either way; these variants only control whether the *script itself* stays attached to catch Ctrl-C. See "Daemon mode" below.

---

### Terminal 1 — Shared infrastructure

```bash
mise run ai-context-infra-up
```

**What it does:**
1. Creates the Docker network `ai-context-net` (if not already present)
2. Starts **Qdrant** (vector store), **PostgreSQL** (file index + L0/L1 cache), and the shared **inference service** (embeddings + re-ranking — mandatory, always started) — all via the standalone `ai-context-infrastructure` Docker Compose project
3. Waits until all containers report healthy
4. Prints a keep-alive message and stays running

> If a piece is already running, the command detects this and leaves it alone — only what's missing gets started.

This infra layer is shared across all projects and all `mise run ai-context-up` terminals, and lives in its own `ai-context-infrastructure` namespace, fully independent of the per-repo `<repo-slug>_context_mcp` / `<repo-slug>_context_watcher` containers. Stop it with **Ctrl+C** — or from any other terminal with `mise run ai-context-infra-down` — only when you are done for the day and no AI context session is active.

---

### Terminal 2 — AI context layer

```bash
mise run ai-context-up
```

**What it does:**

1. **Validate config** — reads `config.yaml`, resolves paths, writes `.ai-context/.resolved.json`
2. **For each project defined in `config.yaml`:**
   - **Check init status** — queries PostgreSQL for an existing registration
   - **If first run for this project:**
     - Register project in PostgreSQL
     - Create Qdrant vector collection
     - Run **full initial index** (blocking — waits for completion before continuing)
     - Generate **L0 project map**
3. **Start the consolidated AI context layer** — one MCP server container and one watcher container for the whole repository
   - The MCP server creates one independent Express app per project, each listening on its own `mcp.port`
   - The watcher creates one independent watchdog task per project, each watching its own `root_path`
4. **Register all MCP servers in IDE configs** (`shared.mcp_config_paths` in `config.yaml` — defaults to `.vscode/mcp.json` and `~/.config/github-copilot/intellij/mcp.json`) — idempotent, safe to re-run. You can extend or replace the list to match the agents you actually use.
5. **Print the summary table and keep running**

**On subsequent runs** (projects already registered), the watcher runs the self-healing startup sequence below for every project before going live. If the configured LLM model is not reachable when the watcher starts, startup aborts before any destructive work (full re-index or SHA-delta reconciliation) can overwrite or corrupt the existing index. Restore the missing model and restart; the previous index remains untouched. (Embeddings are no longer part of this check — the inference service is mandatory, always-on infra with its own container healthcheck, not a user-managed external process.)

### Daemon mode — one terminal instead of two

Each of the two commands above has a `-daemon` task variant:

```bash
mise run ai-context-infra-up-daemon
mise run ai-context-up-daemon
```

Each variant runs the exact same startup sequence as its foreground counterpart, then exits immediately instead of staying attached — no dedicated terminal needed, and no Ctrl-C-triggered shutdown/backup tied to that terminal session. `ai-context-up-daemon` also auto-starts the shared infra in daemon mode internally if it isn't already running, so a single daemon-mode call is enough to bring up the whole stack unattended (e.g. from a script, or a terminal you intend to close). Stop everything the normal way when you're done:

```bash
mise run ai-context-down          # stops MCP servers + watchers, takes a backup
mise run ai-context-infra-down    # stops the shared DB + re-ranker
```

Under the hood these are just `ai-context-infra-up.sh --daemon` / `ai-context-up.sh --daemon` — the flag also works directly (`mise run ai-context-up -- --daemon`) if you're scripting against the underlying command instead of the dedicated task.


**Self-healing startup sequence:**

| Step | What happens |
|------|-------------|
| **1. SHA-delta reconciliation** | Scans all eligible files, compares SHA-256 against the DB — indexes new/changed files, removes deleted ones. Delta-only, unchanged files are skipped. Skipped if the required models are unreachable at startup. |
| **2. Qdrant parity check** | Compares `SUM(chunk_count)` from `file_index` against the actual Qdrant vector count. If counts differ, a per-file scan re-embeds any file with a vector count mismatch. Skipped if the required models are unreachable at startup. |
| **3. L1 summary backfill** | Queries for any `file_index` row with a missing `l1_summaries` entry and generates only the missing summaries. No-op if all are present; skipped if the LLM is unreachable at startup. |
| **4. L0 refresh** | Regenerates the project structure map if any changes were applied in steps 1–3. No LLM required, so it still runs when only summaries are unavailable. |

**Stop when done:** press **Ctrl+C** in this terminal — or from any other terminal run `mise run ai-context-down`. All MCP servers and watchers stop gracefully and a full backup (snapshot + JSON) is taken automatically. The shared infrastructure (Terminal 1) keeps running.

**Expected summary output:**

```
──────────────────────────────────────────────────────────────────────

  ==> MCP server running:

  PROJECT                            URL
  ─────────────────────────────────  ──────────────────────────────────
  my-platform-backend             http://localhost:7100/mcp
  my-platform-frontend            http://localhost:7200/mcp
  my-platform-components          http://localhost:7300/mcp
  my-platform-demo                http://localhost:7400/mcp

  ==> Watcher running:

  CONTAINER                                             DEBOUNCE
  ───────────────────────────────────────────────────   ────────
  my-platform_context_watcher                        multi-project

  ==> Logs:

  docker logs -f my-platform_context_watcher
  docker logs -f my-platform_context_mcp

──────────────────────────────────────────────────────────────────────

  Press Ctrl-C to stop all services.
```

---

### Lifecycle commands

| Command | Terminal | What it does |
|---|---|---|
| `mise run ai-context-infra-up` | 1 (persistent) | Start shared infra — Qdrant + PostgreSQL + re-ranker (if enabled) — via the `ai-context-infrastructure` Docker Compose project; keep-alive until Ctrl+C. Use `ai-context-infra-up-daemon` to return immediately instead |
| `mise run ai-context-up` | 2 (persistent) | Validate config → init each project (if new, with optional backup restore) → start all MCP servers + watchers → keep-alive until Ctrl+C. Use `ai-context-up-daemon` to return immediately instead |
| `mise run ai-context-down` | any | Stop all MCP servers and watchers; takes a full backup (snapshot + JSON) automatically (`--no-backup` to skip) |
| `mise run ai-context-infra-down` | any | Stop the shared infra (Qdrant + Postgres + re-ranker); data volumes are preserved. Pass `--wipe` to also delete all volumes (clean slate), `--stop-services` to stop MCP servers first |
| `mise run ai-context-reset` | any | Wipe all project data, then re-init and restart (runs `clean` for each project, then `up`) |
| `mise run ai-context-backup` | any | Manually trigger a full backup for all projects (snapshot + json + postgres) |
| `mise run ai-context-clean [project]` | any | Remove one project from all DBs entirely and stop its containers — no restart |

**Typical end-of-session shutdown sequence:**

```bash
mise run ai-context-down        # 1. stop watchers + MCP servers (+ full backup)
mise run ai-context-infra-down  # 2. stop Qdrant + PostgreSQL + re-ranker
```

Or in one step (stops services first, then DB):

```bash
mise run ai-context-infra-down -- --stop-services
```

---

### Reset flow

Use **reset** when you need a full re-index from scratch (e.g. after rebuilding the inference service with a different embedding model, or after changing chunker logic or config filters):

```bash
mise run ai-context-reset
```

This runs `clean` (for every project) followed by `up`:

**What `clean` does** (per project):
1. Stops and removes the repo's consolidated `<repo-slug>_context_mcp` and `<repo-slug>_context_watcher` containers
2. Deletes the Qdrant vector collection for the project
3. Deletes the project row from PostgreSQL (cascades to all chunks, L1 summaries, file records)

**What `up` does next:** since all registrations are gone, `up` treats every project as a first-time init and re-runs the full index + L0 generation sequence.

To clean a **single project** without touching the others:

```bash
bash .ai-context/scripts/clean.sh my-platform-backend
# then re-add it:
mise run ai-context-up
```

> `mise run ai-context-up` will detect that `my-platform-backend` is no longer registered and re-init only it, while leaving all other projects' watchers running.

---

## Database Backups

The watcher container automatically maintains a **full, per-project backup** of both the Qdrant vector collection and the PostgreSQL project data. Backups are stored as plain files inside the repository so team members who clone the repo get an up-to-date index for free — no need to run the full initial index.

### Backup location

```
.ai-context/database-backup/
  my-platform-backend/
    qdrant.snapshot      Binary Qdrant collection snapshot (fast restore, gitignored)
    qdrant.vectors.json  Scroll-export of all vectors + payloads (git baseline for teammates)
    postgres.json        All project rows (projects · file_index · l1_summaries · l0_maps · doc_trees)
    backup_meta.json     Timestamp, file count, chunk count, doc_tree_count, trigger, format
  my-platform-frontend/
    ...
```

Only one of `qdrant.snapshot` / `qdrant.vectors.json` may be present, or both (when `backup_format: "both"` is used). When both are present, restore selects the **newer file by mtime**. `backup_meta.json` is **optional** — used for display only (file count, chunk count, creation date); restore does not depend on it.

### Backup triggers

| Trigger | When it fires |
|---|---|
| **On shutdown** | Fires when pressing Ctrl-C in the `ai-context-up` terminal — runs `ai-context-backup.sh --both` for all projects in parallel |
| **On `ai-context-down`** | Fires when calling `mise run ai-context-down` — runs `ai-context-backup.sh --both` (skip with `--no-backup`) |
| **Manual** | `mise run ai-context-backup` — can be called at any time while the shared infrastructure is running |

### Restore at first-run

When `mise run ai-context-up` encounters a project that is **not yet registered** (e.g. after a `mise run ai-context-reset` or on a fresh clone) AND a restorable backup exists (`postgres.json` + at least one Qdrant file), it offers a restore option — accept to restore in seconds instead of re-indexing from scratch (`Restore from backup instead of full re-index? [Y/n]`).

Full configuration reference, manual single-project restore, and exact backup contents (including a known gap — `symbol_index`/`import_edges`/`call_edges` aren't backed up yet): **[Database Backups — Full Reference](docs/backups.md)**.

---

## IDE Integration

`init.sh` — run automatically by `mise run ai-context-up` on first use — writes the MCP server entries to every path configured in `shared.mcp_config_paths`. The default set covers GitHub Copilot's native VS Code plugin and IntelliJ plugin, so no manual steps are needed for those. Any other MCP-compliant agent (e.g. Crush) can be pointed at the same URL manually — see [IDE Integration — Full Reference](docs/ide-integration.md#any-other-mcp-client). (`sst/opencode` is a separate case — see the naming note in the intro above — and is the target of a native plugin under active development; see `opencode-ai-context-plugin/` at the repo root.)

| Client | Default config file | Scope |
|-----|-------------|-------|
| GitHub Copilot — native VS Code plugin | `<repo-root>/.vscode/mcp.json` | Project — commit this file so the whole team gets it |
| GitHub Copilot — IntelliJ plugin (also used by JetBrains AI Assistant setups) | `~/.config/github-copilot/intellij/mcp.json` | Global — all projects share this file |

Use the `shared.mcp_config_paths` list (and a per-project `projects[].mcp_config_paths` override if needed) to add more destinations or remove the defaults. Paths accept `{repo_root}`, a leading `~` for `$HOME`, and the standard project placeholder `{project_name}` in filenames. The entry added to each file looks like:

```json
{
  "servers": {
    "<project-name>-context": {
      "type": "http",
      "url": "http://localhost:<port>/mcp"
    }
  }
}
```

Re-running init is safe — it updates each project's entry without touching unrelated entries, and it writes to every configured `mcp_config_paths` entry.

**After init, restart your IDE** (or toggle the Copilot chat panel) once to pick up the new server.

Full instruction-delivery details, skill-file placement per agent, and manual setup for any other MCP client: **[IDE Integration — Full Reference](docs/ide-integration.md)**.

---

## Available MCP Tools

### `/mcp` endpoint — 5 tools

| Tool | Description |
|------|-------------|
| `repo_structure` | L0 project map + depth-capped directory tree. Optional `dir_path`, `depth`. Call first for orientation. Per-file entries may show `[hub]`/`[bridge]` structural-role tags derived from `import_edges`/`call_edges` degree. |
| `semantic_search` | Identifier fast-path (`symbol_index`) if the query looks like a single identifier, else hybrid/ColBERT vector search. Returns raw ranked hits (file:line, score, L1 summary, `why` provenance note) — no prose condensation. |
| `get_file` | Exact line-range slices from stored chunk text (Qdrant) or doc-tree node `body_text` (PostgreSQL). Optional `start_line`, `end_line`, `max_chars`. Reports coverage gaps and detects directories; never reads the project file from disk. |
| `analyze_impact` | Blast-radius traversal over `call_edges`/`import_edges` (PostgreSQL). Required `symbol_name`, `direction` (`callers`\|`callees`\|`both`); optional `file_path` (disambiguation), `depth` (1–5, default 1), `include_tests` (default true). Returns direct callers/callees, ambiguous call targets, likely-covering test files, and a `blast_radius_summary`. |
| `resolve_symbol` | Compiler-grade `definition`/`references`/`hover` via a real language server (see [LSP Integration](docs/lsp-integration.md)), for when `analyze_impact`'s heuristic call graph isn't precise enough. Required `file_path`, `line` (1-based), `character` (0-based), `kind` (`definition`\|`references`\|`hover`). Only available for languages **auto-detected** as present in that project (`lsp.enabled_languages` is derived from the project's files, not a config field — same principle as `profile`) — otherwise returns a message explaining why, not an error. |

### `/mcp-admin` endpoint — 2 operator-only tools

Not part of the agent-facing manifest or the skill — for operators/debugging only.

| Tool | Description |
|------|-------------|
| `list_recent_changes` | Lists recently modified/indexed files. `limit` is bounded 1–200 (default 10). |
| `get_index_status` | Compact health snapshot: file/chunk counts, L1 coverage, Qdrant status, index freshness, doc tree counts. |

## Available HTTP Endpoints

> Both `/mcp` and `/mcp-admin` are **MCP StreamableHTTP** endpoints (MCP protocol over HTTP). The remaining endpoints (`/health`, `/search`, `/instructions`) are plain REST.

| Endpoint | Protocol | Description |
|----------|----------|-------------|
| `ALL /mcp` | MCP | `repo_structure` + `semantic_search` + `get_file` + `analyze_impact` + `resolve_symbol` only. **Admin tools are blocked here.** |
| `ALL /mcp-admin` | MCP | Always exposes `list_recent_changes` + `get_index_status` (operator-only). Use for debugging and admin operations. |
| `GET /health` | REST | Health check — returns project key and sources count |
| `GET /search?q=<query>` | REST | Plain-text ranked chunk hits for CLI/scripting debugging — thin wrapper over the same `rankedSemanticSearch()` the `semantic_search` MCP tool uses (single project only, no L0/L1 assembly, no cross-project `sources` expansion) |
| `POST /search` | REST | Same as above, body: `{ "query": "…" }` |
| `GET /instructions` | REST | Serves `.ai-context/instructions.md` with placeholder substitution, or a generated default — re-read fresh on every request |

## CLI search (for debugging and scripting)

Test retrieval quality directly from the terminal while the MCP servers are running:

```bash
# Query the first project (auto-detected from .resolved.json)
bash .ai-context/scripts/search.sh "user authentication and JWT"

# Query a specific project
bash .ai-context/scripts/search.sh "PlatformUserRepository" my-platform-backend

# Raw curl (same endpoint)
curl -s "http://localhost:7100/search?q=Koin+module+injection"
curl -s -X POST "http://localhost:7100/search" \
  -H "Content-Type: application/json" \
  -d '{"query":"Koin module injection"}'
```

Response headers carry metadata:

| Header | Value |
|--------|-------|
| `X-Chunk-Count` | Number of ranked hits returned |

(Earlier docs also listed `X-Estimated-Tokens` and `X-Sources` headers and described this endpoint as assembling L0+L1+L2 context — that described `assembleContext()`, a function that exists in `context_assembler.ts` but is not wired to this route. The actual `/search` handler only calls `rankedSemanticSearch()` and sets `X-Chunk-Count`.)

---

## Benchmark Results

Full methodology, tool mapping, query set, and per-query results:
[`docs/benchmark/benchmark-results.md`](docs/benchmark/benchmark-results.md).

**What was tested:** the new 3-tool agent surface (`repo_structure`,
`semantic_search`, `get_file`) against `idea-mcp` (IntelliJ's JetBrains MCP
server), using the same 10 real questions about this repository for both.

**How:** each tool's closest-fit equivalents were mapped by *purpose*
(orient / find / read), not by name (see the linked doc's tool-mapping
table), then run live via native tool calls — no curl/SSE scripting — with
tool-call count, result noise, and manual `file:line` correctness spot-checks
recorded per query.

**Verdict: go.** Both sides averaged an identical 1.3 tool calls/query, so
`ai-context`'s ranked `semantic_search` output did not cost extra round-trips
despite being the richer surface. Key findings:

- **Prose queries are `ai-context`'s clear win.** All 4 queries idea-mcp had
  to manually translate into guessed identifiers first were answerable
  directly from the natural-language question via `semantic_search` — the
  single biggest structural gap idea-mcp has (no vector-search fallback).
- **Ranked/summarized results avoid the noise problem.** idea-mcp's
  unscoped `search_text` fallback returned 18–83 raw hits per query in 2 of
  10 cases; `semantic_search` never returned more than 9 ranked hits with
  L1 summaries attached, even on the broadest queries.
- **Dispatch-table-style code (string-keyed `switch`/`case` handlers) is a
  blind spot for both tools** — neither indexes control-flow branches as
  addressable units, so both needed a manual file read to find a value
  computed inside a `case` arm rather than a named function.
- **Ranking can be inconsistent across near-duplicate query phrasings** —
  the single most relevant chunk for one question ranked top-3 under one
  phrasing but didn't surface at all under a semantically adjacent
  rephrasing of the same underlying question.
- **Project-scope boundaries are absolute** — `ai-context` only sees files
  inside its indexed project directory; a script one level up in the outer
  repo was invisible to it, whereas idea-mcp's IDE-wide indexing found a
  related file directly. This is a deliberate isolation tradeoff, not a bug.

---

## Testing

Three independently-runnable tiers (unit / integration / e2e), so a change
can be verified at a cost matching its blast radius:

```bash
scripts/test.sh unit          # seconds, no Docker
scripts/test.sh integration   # adds disposable Postgres/Qdrant
scripts/test.sh e2e           # adds the full real pipeline, minutes
```

See `docs/testing.md` for the full guide: which tier to run for which
change, the shared multi-language fixture corpus, environment setup, and
cleanup (`scripts/test-cleanup.sh` — every Docker resource these tests
create is labeled and safe to run alongside your real dev stack).

---

## Troubleshooting

**"Shared infrastructure not running"**

```bash
mise run ai-context-infra-up
```

**L1 summaries not generating**

- Check LM Studio (or your configured `llm.provider`) is running with the server enabled
- Or switch `llm.provider` to `ollama` in `config.yaml`
- Embedding errors are no longer a config/provider issue — check `docker ps` for `ai_context_inference` instead; see [Troubleshooting — Full Reference](docs/troubleshooting.md)

**Qdrant collection not found / project not registered, index corruption, or DB schema upgrades**

```bash
mise run ai-context-reset   # full wipe and re-init from scratch
```

Every other documented issue (thinking-model latency, when a full re-index is actually needed vs. the self-healing watcher already handling it, log locations, DB schema upgrade steps): **[Troubleshooting — Full Reference](docs/troubleshooting.md)**.

---

## Learn more

This README is the tight entry point. Every topic below has a deeper companion doc:

**Written reference docs (`docs/`):**

| Doc | Covers |
|---|---|
| [`concepts-and-prior-art.md`](docs/concepts-and-prior-art.md) | The papers and prior-art repositories this design draws on, and how this project's approach differs |
| [`architecture-deep-dive.md`](docs/architecture-deep-dive.md) | Retrieval layers, query flow, MCP manifest design decisions, re-ranking, query expansion, symbol index, call/import graph + `analyze_impact`, structure-aware chunking, file-watcher event handling and reliability |
| [`lsp-integration.md`](docs/lsp-integration.md) | `resolve_symbol`/LSP Bridge architecture, per-language support matrix, licensing (incl. the Java/EPL-2.0 analysis), and dependency-resolution ("jump into external dependency code") per language |
| [`configuration.md`](docs/configuration.md) | Full `config.yaml` field reference, profile auto-detection schema, monorepo example, cross-project `sources` federation |
| [`backups.md`](docs/backups.md) | Full backup/restore reference, manual single-project restore, exact backup contents |
| [`ide-integration.md`](docs/ide-integration.md) | Instruction-delivery details, skill-file placement per agent, manual setup for any other MCP client |
| [`troubleshooting.md`](docs/troubleshooting.md) | Every documented issue and fix |
| [`testing.md`](docs/testing.md) | Test tiers, fixture corpus, environment setup, cleanup |
| [`benchmark/benchmark-results.md`](docs/benchmark/benchmark-results.md) | Full benchmark methodology, tool mapping, query set, per-query results |
| [`next-plans/`](docs/next-plans/) | The active backlog/roadmap plus design plans for features not yet built |

**Interactive visual docs (`docs/*.html`, open directly in a browser):**

| Doc | Covers |
|---|---|
| [`overview.html`](docs/interactive/overview.html) | Problem, key benefits, the three retrieval layers, a query from start to finish, main components, privacy boundary |
| [`chunking.html`](docs/interactive/chunking.html) | Structure-aware chunking strategy per file type, interactive explorer |
| [`context-map.html`](docs/interactive/context-map.html) | Re-ranking, query expansion, symbol index, call/import graph, interactive retrieval-pipeline explorer |
| [`infrastructure.html`](docs/interactive/infrastructure.html) | Container/topology map, watcher self-healing |
| [`sequences.html`](docs/interactive/sequences.html) | Interactive sequence diagrams for query flow and watcher event handling |
