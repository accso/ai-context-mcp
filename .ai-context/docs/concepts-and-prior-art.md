# Concepts & Prior Art

This project sits at the intersection of two research threads usually discussed
separately — agent-driven iterative retrieval, and repository-level code
intelligence — plus a set of reference implementations whose patterns we adopted
directly.

> **None of the repositories below are used by this project.** They are not
> dependencies, are not installed, and none of their code is vendored or run.
> We only studied them and re-implemented ideas from them in our own
> codebase. Where a section says "we adopted" something, it means *the idea*,
> reimplemented independently — not the tool itself. The same applies to the
> papers above.

## Papers

| Paper | Relevance to us |
|---|---|
| **Agentic Retrieval-Augmented Generation: A Survey on Agentic RAG** — Singh et al., 2025 ([arXiv:2501.09136](https://arxiv.org/abs/2501.09136)) | Defines the paradigm shift from static pipeline RAG to **Agentic RAG**: an agent that owns the retrieval loop — deciding which tool to call, when to stop, and how to combine partial evidence. Motivates letting the *calling* coding agent drive `repo_structure → semantic_search → get_file` itself instead of routing through a second, local LLM. |
| **A-RAG: Scaling Agentic Retrieval-Augmented Generation via Hierarchical Retrieval Interfaces** — Du et al., 2026 ([arXiv:2602.03442](https://arxiv.org/abs/2602.03442)) | Exposes hierarchical retrieval interfaces (keyword search, semantic search, chunk read) directly to the model so the agent decides what to retrieve at which granularity, instead of a one-shot or predefined workflow. Closest published match to our agent-driven `repo_structure → semantic_search → get_file` loop and its token-efficiency goal. |
| **One Tool Is Enough: Reinforcement Learning of LLM Agents for Repository-Level Code Navigation** (RepoNavigator) — ICML 2026 ([arXiv:2512.20957](https://arxiv.org/abs/2512.20957)) | A single language-server-backed `jump` tool that returns a symbol's exact definition. Supports the idea behind `resolve_symbol` (compiler-grade navigation via LSP). We deliberately keep several tools and do no reinforcement-learning training. |
| **A Survey on Reasoning Agentic Retrieval-Augmented Generation** — 2025 ([arXiv:2506.10408](https://arxiv.org/html/2506.10408v1)) | Retrieval and reasoning interleaved turn-by-turn, with a bounded reasoning-retrieval loop as a recognised pattern rather than an ad-hoc shortcut — supports the skill's "max ~2 iterations" guardrail. |
| **Retrieval-Augmented Code Generation: A Survey with Focus on Repository-Level Approaches** — 2025 ([arXiv:2510.04905](https://arxiv.org/html/2510.04905v1)) | Shows multi-granularity retrieval (file / class / function / statement) consistently outperforms single-level chunk search for repo-scale codebases — justifies keeping our L0 (structure) / L1 (summary) / L2 (chunk) layering. |
| **CodeRAG: Finding Relevant and Necessary Knowledge for Retrieval-Augmented Repository-Level Code Completion** — EMNLP 2025 ([paper](https://lihui.info/doc/EMNLP25.pdf)) | Necessity-driven retrieval over a requirement graph, instead of blind top-k similarity — supports our AST/symbol-index approach (`symbol_index` table) over pure embedding search for identifier-precise lookups. |
| **GraphCodeAgent: Dual Graph-Guided LLM Agent for Retrieval-Augmented Repo-Level Code Generation** ([arXiv:2504.10046](https://arxiv.org/abs/2504.10046)) | An agent (not a fixed pipeline) navigates a structure + dependency graph to retrieve code — the closest published system to letting the agent decide which endpoint to call and when, using a code graph instead of our `repo_structure` tool. |
| **On Improving Repository-Level Code QA for LLMs** ([Uni Hamburg, 2024](https://www.inf.uni-hamburg.de/en/inst/ab/lt/publications/2024-strichetal-aclsrw.pdf)) | Confirms summary-first, chunk-second retrieval reduces hallucinated code references — validates L1 summaries as a cheap first pass before L2 chunk retrieval. |
| **KGCompass — Enhancing Repository-Level Software Repair via Repository-Aware Knowledge Graphs** ([arXiv:2503.21710](https://arxiv.org/abs/2503.21710)) | Narrows the search space via multi-hop graph paths before repair — same intuition as our L0 map + symbol index narrowing the search space structurally before semantic search. |
| **GraphSearch: An Agentic Deep Searching Workflow for Graph Retrieval-Augmented Generation** ([arXiv:2509.22009](https://arxiv.org/abs/2509.22009)) | Dual-channel retrieval (semantic + relational), issued iteratively by the agent — directly analogous to `semantic_search` (chunk channel) alongside `repo_structure` (structural channel) under one agent-driven loop. |
| **A2RAG: Adaptive Agentic Graph Retrieval for Cost-Aware and Reliable Reasoning** ([arXiv:2601.21162](https://arxiv.org/abs/2601.21162)) | An adaptive controller stops retrieval once evidence is sufficient, instead of always running a fixed number of rounds — supports the skill's "stop as soon as you have enough context" rule. |
| **The Navigation Paradox in Large-Context Agentic Coding** ("CodeCompass") ([arXiv:2602.20048](https://arxiv.org/html/2602.20048v1)) | For architecture-heavy tasks, graph-structured dependency navigation outperforms flat retrieval — the trigger condition for a possible future code-knowledge-graph iteration, deliberately deferred and not part of the current scope. |
| **Context-Augmented Code Generation Using Programming Knowledge Graphs** ([arXiv:2410.18251](https://arxiv.org/abs/2410.18251)) | Fine-grained, tree-pruned code retrieval plus re-ranking reduces hallucination — reinforces our AST-boundary chunking and ColBERT re-ranking choices. |
| **LARGER: Lexically Anchored Repository Graph Exploration and Retrieval** ([arXiv:2605.16352](https://arxiv.org/abs/2605.16352)) | Anchors lexical matches to graph nodes, then does confidence-filtered local expansion **inside the agent's existing search loop** — supports keeping any future structural retrieval inside the same 3-tool loop rather than a separate graph-specific tool. |

## Repositories (inspiration only — not used)

### [byterover-cli](https://github.com/campfirein/byterover-cli)
*Not used — only the idea was reimplemented.*

A CLI tool that continuously watches a codebase, chunks source files, embeds them, and stores the vectors in a local vector database. It proves the core pipeline: **file watcher → chunker → embedder → vector store**. We reimplemented this pattern (independently, without using byterover-cli) as the backbone of our indexer and watcher components and extend it with language-aware boundary-detection chunking (Kotlin, TypeScript/JS, Python, C#, Java, Swift, Go, Rust, Ruby, PHP, C/C++, Vue SFC, SQL, YAML/TOML) and a PostgreSQL-backed file hash registry for true delta indexing.

### [rlm — Retrieval-augmented Language Models](https://github.com/alexzhang13/rlm)
*Not used — only the idea was reimplemented.*

A research-oriented framework for tiered retrieval: the idea that context should be assembled at multiple granularity levels and that each level serves a different purpose. Coarse structural information (what modules exist, where entry points are) anchors the response, while fine-grained semantic chunks fill in the detail. We map this directly to our **L0 / L1 / L2 retrieval layers** and adopt the score-threshold cross-project search logic from its multi-corpus retrieval approach.

### [OpenViking](https://github.com/volcengine/OpenViking)
*Not used — we do not run, depend on, or embed OpenViking. Only the architectural idea was reimplemented in our own MCP server.*

An MCP (Model Context Protocol) server implementation that shows how a tool-calling layer can sit between an AI assistant and a retrieval backend. It demonstrates the request-routing pattern, health-checking, and SSE transport that make an MCP server compatible with clients like GitHub Copilot and IntelliJ-based assistants. Our MCP server layer is modelled on this architecture, extended to support the tiered L0/L1/L2 response format and cross-project source federation.

### [mcp-local-rag](https://github.com/shinpr/mcp-local-rag)
*Not used — we do not run or depend on mcp-local-rag; the ideas below were reimplemented in our own code.*

A local RAG MCP server that adds a **keyword boost layer on top of semantic search**. Pure vector similarity can rank exact identifiers (class names like `PlatformUserRepository`, annotations like `@KoinComponent`, Gradle config keys) lower than they deserve because embedding models abstract them away. We adopted the hybrid scoring idea: after Qdrant returns semantic hits, a lightweight term-frequency keyword boost scores each chunk against the raw query terms (including camelCase sub-tokens). The final rank is a weighted blend — 75% semantic + 25% keyword — ensuring exact symbol matches reliably surface at the top without sacrificing semantic relevance.

We also adopted the concept of **structure-aware chunking for non-code files**. While `mcp-local-rag` uses full embedding-similarity sentence chunking (expensive), we applied the same goal with zero embedding overhead: Markdown files split at heading boundaries, SQL files at statement boundaries (`;`), YAML/TOML at top-level keys, and Python files at `class`/`def` boundaries — all dispatched by file extension in `chunker.py` using Python's `match/case`. C# (`.cs`), Java (`.java`), Swift (`.swift`), C/C++, Go, Rust, Ruby, and PHP get their own boundary-detection regex patterns and are also registered in the index extension list.

`mcp-local-rag` also exposes a `status` tool and a CLI query mode. We adopted both: `get_index_status` is an admin-only MCP tool (see `/mcp-admin`) returning a compact health snapshot (file count, L2 chunk count, L1 coverage, Qdrant vector count and collection status, index freshness) without requiring Docker access. The CLI mode is implemented as `scripts/search.sh` calling a `GET/POST /search` REST endpoint on the Express server — simpler than driving the full MCP JSON-RPC handshake from shell, and consistent with the existing `/health` and `/instructions` routes. A `delete_file` MCP tool was deliberately not adopted: the file watcher already handles filesystem deletions automatically and atomically, and forced index removal without a filesystem change is a niche admin case better handled via `extra_skip_paths` + `mise run ai-context-reset`.

### [mcp-rag-server](https://github.com/Daniel-Barta/mcp-rag-server)
*Not used — we do not run or depend on mcp-rag-server; the ideas below were reimplemented in our own code.*

A RAG MCP server that exposes a `GET /instructions` endpoint, serving a configurable Markdown file that tells the AI assistant how to use the available tools. We adopted this pattern: our MCP server now exposes a `GET /instructions` route that reads `.ai-context/instructions.md` from the repository root (with `{project_name}` and `{display_name}` placeholder substitution) and falls back to a generated default when no file exists. This lets teams document project-specific conventions — preferred tools, module ownership, coding standards — without modifying code.

The `list_files` browsing tool from `mcp-rag-server` is also implemented here; it maps to our `repo_structure` tool, which queries the `file_index` PostgreSQL table and supports directory/depth filtering.

### [PageIndex](https://github.com/VectifyAI/PageIndex)
*Not used — we do not run, depend on, or include PageIndex. Only the tree-index idea was reimplemented in our own code (`doc_tree_builder.py`, `doc_trees` table).*

A vectorless, reasoning-based RAG system by VectifyAI that builds a hierarchical tree index ("Table of Contents") from long documents and uses LLM reasoning to navigate them — simulating how a human expert would. Unlike semantic similarity search, PageIndex retrieval is traceable, context-aware, and section-anchored. We adopted the tree-index concept selectively for long-form documentation files (`.md` docs, ADRs, RFCs, wikis) where narrative structure and reasoning-based navigation outperform chunk-level vector similarity. Source code continues to use Qdrant + hybrid scoring; `get_file` auto-routes to whichever representation the requested path was indexed as.
