# Project Configuration — Full Reference

All projects are defined in **`.ai-context/config.yaml`**. Each entry in the `projects` list is one independently indexed and served context layer with its own Qdrant collection and MCP server.

For the essentials (key fields + a minimal single-project example), see the [`README.md`](../README.md#project-configuration) — this doc adds the full field reference plus multi-project/monorepo and cross-project-sources configuration.

> **`sst/opencode` users:** `config.yaml`'s `projects` list, ports, and
> per-project Qdrant collections are specific to this Docker stack (now
> "heavy mode" for large repos/other agents). The native
> [`opencode-ai-context-plugin/`](../../opencode-ai-context-plugin/) has no
> equivalent config file — it has no multi-project registry (one SQLite
> store per worktree, auto-derived) and configures its handful of knobs
> (heavy-mode opt-in, model cache dir, Matryoshka output dims) via
> `AI_CONTEXT_PLUGIN_*` environment variables instead — see its
> [`README.md`](../../opencode-ai-context-plugin/README.md#design-choices-worth-calling-out).

## Key fields

| Field | Required | Description |
|---|---|---|
| `name` | ✅ | Kebab-case unique key — becomes the Qdrant collection, DB key, and `mcp.json` server key |
| `display_name` | ✅ | Human-readable label shown in tool descriptions |
| `profile` | — (auto) | Structured technology-mix object, **auto-detected** from the project's files — not a config field; setting it in `config.yaml` is a validation error. See "Profile auto-detection" below for the full schema. |
| `lsp.idle_timeout_minutes` | — | Optional per-project override (default **15**, `scripts/validate_config.py:DEFAULT_LSP_IDLE_TIMEOUT_MINUTES`) for how long an idle per-`(project, language)` language-server process is kept warm in the LSP Bridge pool before eviction. A performance knob only — never a correctness choice, since `lsp.enabled_languages` itself is always auto-detected (see `resolve_symbol` in [Architecture Deep-Dive](architecture-deep-dive.md)), never hand-set. |
| `lsp.accept_java_epl_license` | — | Default **false**. Unlike every other language, Java is **not** auto-enabled just because `.java` files are detected — enabling it downloads and runs the EPL-2.0-licensed Eclipse JDT Language Server directly from Red Hat's servers at Docker-build time. Set to `true` to explicitly opt in, or answer the interactive prompt `mise run ai-context-up` shows the first time it detects Java files (it persists your answer as a local `.java-epl-accepted-<project>` marker file instead — see `scripts/ai-context-up.sh`). Skipped/defaults to disabled in non-interactive environments (CI, no TTY). See "Licensing — Java / EPL-2.0" in [LSP Integration](lsp-integration.md) and `THIRD-PARTY-NOTICES.md` for the full rationale. |
| `root_path` | ✅ | Path to the project root, relative to the repo root. Use `""` for the whole repo |
| `mcp.port` | ✅ | Port for this project's MCP server. **Must be set manually** — pick a free port unique per machine |
| `mcp.max_context_tokens` | — | Token budget for `assembleContext()`'s L0+L1+L2 assembly (default `8000`) — **not currently consumed by any live route**; `semantic_search` and `/search` both use `rankedSemanticSearch`, which ignores this field. Reserved for if/when `assembleContext()` is wired back to a route. |

> **Project-scope boundary — files outside `root_path` are never indexed or
> answerable, even if they are operationally related.** Every retrieval
> path (`semantic_search`, `get_file`, `repo_structure`, and the CLI
> `/search`) is scoped strictly to a project's configured `root_path` — the
> watcher container only ever has that directory mounted (see "File watcher
> event handling" in [Architecture Deep-Dive](architecture-deep-dive.md)), so
> nothing outside it is visible to that project's MCP server at all. This
> is a deliberate isolation boundary, not a bug, and there is currently no
> cross-boundary fallback (`sources` federation is the supported way to
> pull in another *project's* context — see "Cross-project sources" below —
> but it still only ever reads from another project's own `root_path`,
> never from arbitrary paths outside any registered project).
>
> A common way to be surprised by this: this repository's own
> `ai-context` project (self-hosted, `root_path: ".ai-context"`) cannot
> answer questions about `scripts/ai-context-reset.sh` — that script lives
> one directory *above* `.ai-context/`, at the actual repo root, so it's
> outside this project's `root_path` and is never indexed. Anything at or
> above a project's configured root, however operationally close, needs
> either a project registered with a `root_path` that covers it, or to be
> asked about via a different tool/means entirely.

## Profile auto-detection

`profile` is a structured, extensible object, not a flat enum string — this
replaced an earlier `"kotlin-backend"`/`"react-frontend"`/`"mixed"`-style
string specifically because a flat enum can't represent a monorepo with
more than one real language/layer, and because `"mixed"` used to conflate
two different situations ("genuinely 2+ domains detected" vs. "nothing
matched, we don't know what this is").

Shape:

```jsonc
{
  "components": [
    { "language": "kotlin",     "layer": "backend",  "framework": null,    "build_system": "gradle" },
    { "language": "typescript", "layer": "frontend",  "framework": "react", "build_system": "npm" }
  ],
  "monorepo": true   // = components.length > 1, kept explicit for readability/logging
}
```

- **`layer`**, not `repo` — the dimension being described is architectural
  layer (`"backend"` | `"frontend"` | `"mixed"`), not the repo itself; a
  monorepo has one repo but multiple `layer`s. `"mixed"` on a single
  component means that one component spans both (e.g. Kotlin Multiplatform
  targeting `commonMain`/`androidMain`/`iosMain`) — different from
  `monorepo: true`, which means multiple *separate* components were found.
- **A list of components, not a single object + special-case flag** — a
  single-stack repo is just `components` with one entry; a monorepo is
  `components.length > 1`. No consumer needs to privilege one component
  over another — both `context_assembler.ts` and `l0_generator.py` iterate
  "for every domain present, do X".
- **`framework`/`build_system` are optional/nullable** specifically so
  future attributes (`test_framework`, `orm`, …) can be added the same way,
  without inventing new compound string enum values.
- **Fallback for "nothing detected"**: `components: [{"language": "unknown", "layer": "mixed", "framework": null, "build_system": null}]`, `monorepo: false` — an explicit "we don't know" instead of silently reusing `"mixed"`.
- **No config.yaml field** — same principle as before: derived from the
  project's actual files (Gradle/Maven build files, `package.json`
  framework deps, file-extension census) at validate time, not typed by a
  human. `scripts/validate_config.py` rejects a manually-set `profile`
  field.

**Consumers:** `context_assembler.ts`'s `expandQueries()` (per-component
query-expansion synonyms — Kotlin/Koin/Ktor, Java/Spring, React/Angular/Vue)
and `l0_generator.py` (which framework-specific parser to run per component
— Gradle module graph, Maven modules, npm workspaces, …). See
`scripts/validate_config.py:_detect_profile` for the detection logic.

## Minimal single-project example

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

Note: `profile` is auto-detected — see the table above. It is not set here.

There is no `embedding:` block. Embeddings (and re-ranking) are generated by the shared, mandatory `ai_context_inference` service (`.ai-context/inference/`, FastEmbed/onnxruntime) — always-on infra, not a config.yaml-configurable provider/model the way the LLM still is. The embedding model is hardcoded because its vector dimension is baked into every project's Qdrant collection schema at creation time — changing it is a data-migration decision (`mise run ai-context-reset`), not a config toggle. See [Architecture Deep-Dive — Embedding service](architecture-deep-dive.md#embedding-service).

## Multi-project monorepo

When working in a monorepo you define a project per logical boundary. Each project gets its own index and MCP server. Use `sources` to let one project pull in context from another at query time — without merging their indexes.

```yaml
projects:
  # ── Core platform backend library ────────────────────────────────────────────
  - name: "my-platform-backend"
    display_name: "Platform Backend"
    root_path: "platform/platform-backend"
    mcp:
      port: 7100

  # ── Demo application — sources the platform backend ──────────────────────────
  - name: "my-platform-demo"
    display_name: "Accso Platform Demo"
    root_path: "demo"
    mcp:
      port: 7400
      max_context_tokens: 10000
    sources:
      - project: "my-platform-backend"
        mode: "l0+l1"
        when: "backend, api, rest, event, persistence, security"
```

When Copilot asks the demo project a question that contains any of the `when` keywords, the backend's L0 map and file summaries are automatically included in the context — no manual file dragging required.

## Cross-project sources

The `sources` block on a project tells its MCP server to also search other registered projects during a query.

| Field | Description |
|---|---|
| `project` | Name of the source project (must be registered and indexed) |
| `scope` | Optional sub-path within the source to restrict results (e.g. `"modules/api"`) |
| `mode` | How much context to include: `l0` (map only) · `l0+l1` (map + summaries, **default**) · `l0+l1+l2` (map + summaries + raw chunks) |
| `when` | Comma-separated keywords — if any appears in the query the source is always included. Leave empty to use automatic similarity routing (threshold 0.55) |

**Requirements for cross-project federation:**
- Both projects must have been initialised (`mise run ai-context-up` run once for each)
- The source project's MCP server must be running when the consuming project's server starts
- Source depth is capped at 1 — sources-of-sources are never followed
