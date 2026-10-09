# Consolidated Backlog + Serena Competitive Finding — Roadmap

> **Verified against the actual codebase (2026-09-01/02).** Every item below
> was checked by grepping/reading the real implementation, not assumed from
> the plan docs. Two corrections to what the source documents implied:
> 1. `lsp-dependency-resolution-plan.md` reads as "Phase 1 (Go+Rust) only,
>    Phases 3-5 gated on a spike" — but `lsp-bridge/dependency_restore.py`
>    (448 lines) already implements the restore step for **all** of
>    Go/Rust/Ruby/Java/Kotlin/C#/Swift/Python/Dart. This plan is
>    materially further along than its own doc suggests. **Not deleted**
>    despite being complete — 6 files (`lsp-bridge/*.py`,
>    `scripts/validate_config.py`) reference it by path in comments as
>    their design-rationale source.
> 2. The `get_file` chunk-slicing P0 fix (section D of the old log) is
>    confirmed shipped — `mcp-server/src/range_slice.ts` now slices to the
>    exact requested line range.
>
> Every item in §1 below was individually confirmed **not** implemented as
> of this check (see inline `Verified:` notes) — the extraction from the
> stale log was accurate for those.
>
> **2026-09-02 cleanup:** `decision-log-combined.md` has now been **deleted**
> (not just marked superseded) — every still-open item was already migrated
> here in the prior pass, it had zero code references, and its content was
> fully absorbed. `call-graph-and-tier1-chunking-plan.md`,
> `lsp-dependency-resolution-plan.md`, `lsp-integration-plan.md`, and
> `structured-profile-plan.md` are **also functionally complete** but were
> **kept at the time** (not deleted) — each was referenced by path from
> multiple source-code comments as the canonical design-rationale document
> for that code.
>
> **2026-09-03 re-audit:** re-confirmed all four "done" plans above are
> still shipped (no regressions). On review, "kept because code references
> it" was the wrong resolution for a *done* plan — a shipped feature's
> rationale belongs in permanent documentation or inline code comments, not
> in a `next-plans` file that's supposed to be disposable. Fixed properly
> instead:
> - Created **[`docs/lsp-integration.md`](../lsp-integration.md)** — a
>   permanent reference doc consolidating the still-relevant architecture,
>   per-language support matrix, full licensing analysis (including the
>   Java/EPL-2.0 legal-review flag, now recorded there instead), and
>   dependency-resolution details from both `lsp-integration-plan.md` and
>   `lsp-dependency-resolution-plan.md`.
> - Folded `structured-profile-plan.md`'s schema/rationale into a new
>   "Profile auto-detection" section in
>   **[`docs/configuration.md`](../configuration.md)**.
> - Updated every code comment across `watcher/*.py`, `mcp-server/src/*.ts`,
>   `lsp-bridge/*.py`, `scripts/validate_config.py`, `scripts/start.sh`,
>   `lsp-bridge/Dockerfile`, `infrastructure/init.sql`, and `config.yaml`
>   (47 references total) to point at these permanent docs (or
>   `docs/architecture-deep-dive.md`, which already covered the
>   call-graph/chunking content) instead of the `next-plans` files.
> - **All four plan files have now been deleted** — verified zero remaining
>   code/doc references before deletion, `npm run build` and the Python
>   unit test suite both still pass (see §0 note below).
>
> Also fixed the one remaining stale "OpenCode" reference in
> `remote-repo-server-plan.md` (Phase 0 hygiene item, now done — see §3).
> Added a new subsection (end of §3) covering
> `external-docs-and-dependency-surface-plan.md` and
> `remote-repo-server-plan.md` — two standalone, not-yet-implemented plan
> files that described real proposed work but weren't tracked anywhere in
> this roadmap; both confirmed via code search to still have zero
> implementation.

## 0. Why this document exists

`decision-log-combined.md` is no longer an actively maintained/tracked
document — most of its entries have already been either **shipped** (their
design rationale now lives in `docs/architecture-deep-dive.md`,
`docs/lsp-integration.md`, and `docs/configuration.md` — see the
2026-09-03 re-audit note above for how those replaced the now-deleted
`call-graph-and-tier1-chunking-plan.md`/`lsp-integration-plan.md`/
`lsp-dependency-resolution-plan.md`/`structured-profile-plan.md`) or
**superseded** into `decision-history-and-adr-plan.md` (still proposed,
not yet built).
What's left behind in that log is a mix of real still-open items with no
dedicated plan file of their own, low-priority/explore-only notes, and one
explicitly rejected option — all at risk of being silently lost now that the
log itself isn't being kept current.

This document does two things:

1. **Extracts every still-open, not-yet-homed item** from
   `decision-log-combined.md` (skipping anything already shipped or
   superseded into another plan — those remain tracked in their own files,
   not duplicated here).
2. **Folds in the Serena competitive finding** from the September 2026
   landscape review (session finding, not yet in any tracked document) —
   the most active, fully-local competitor to `.ai-context`'s LSP-bridge
   layer.

Both are combined into one sequenced roadmap (§3) with an explicit
cost/benefit-vs-current-solution column, so future work can be picked up
from here instead of re-mining the stale log.

`decision-log-combined.md` itself should be marked superseded by this
document (see note added at its top) and kept only for historical
provenance — do not add new items to it.

---

## 1. Still-open items carried forward from `decision-log-combined.md`

Only items with **no dedicated plan file** and **not marked
shipped/superseded** in the original log are listed. Original ID kept in
parentheses for traceability.

| Item | What it is | Decision/work needed | Verified status (2026-09-01) | Source |
|---|---|---|---|---|
| React hook sub-chunking (A.5) | Sub-chunk `useEffect`/`useMemo`/custom-hook bodies ≥4 lines out of an already-produced `.tsx`/`.jsx` component chunk | **Approved, unblocked** (Tier‑1 tree-sitter chunking shipped) — ready to scope into an implementation plan | ❌ Not implemented — `chunk_type: "component"` exists (`chunker.py:292,1017`) but no hook-specific sub-chunking logic (`useEffect`/`useMemo`/`use[A-Z]` — no matches beyond docstrings) | JetBrains "Embark" comparison |
| Backup/restore hardening (B.1) | Compress committed `qdrant.vectors.json`; add `.gitattributes` merge strategy for backup files | Decision: worth it at all, and if so compression first or merge-conflict avoidance first? | ❌ Not implemented — no `gzip`/`compress`/`.gitattributes` reference anywhere in `watcher/backup.py` | codebase-memory-mcp |
| RRF vs. keyword-blend reranking (B.2) | Add a second, independent lexical (BM25/sparse) retrieval channel and fuse via RRF | Needs a controlled benchmark experiment against a query set **before** any production change — not a drop-in swap, current design has only one retrieval channel | ❌ Not implemented — confirmed still single-channel: `context_assembler.ts` does Qdrant vector search + reranker service only, no BM25/sparse/RRF anywhere in `mcp-server/src/*.ts` | code-memory, codebase-memory-mcp, agentmemory |
| Nested `.gitignore` handling (B.3) | Auto-discover nested `.gitignore` files instead of requiring explicit `extra_gitignore_paths` | Decision: worth doing at all? Current single-merged-pathspec behavior is a deliberate, documented simplicity tradeoff, not a bug | ❌ Not implemented — `indexer.py:load_gitignore` still merges root `.gitignore` + configured `extra_gitignore_paths` only, as documented | code-memory |
| Parallelize `run_full_index()` (B.4) | Concurrent per-file indexing pipelines | **Measure first**: add per-stage timing (chunk/embed/summarize/DB-write); test whether the inference service (embeddings) or LM Studio/omlx (LLM) serializes concurrent requests before deciding what (if anything) to parallelize | ❌ Not implemented — no `ThreadPoolExecutor`/`asyncio.gather`/concurrency in `watcher/indexer.py` or `watcher.py`; still single-threaded, no timing instrumentation added either | code-memory |
| Retrieval-quality presets (B.7) | Coarse presets (Code-precise/Doc-accurate/Speed) and/or per-language-profile tuning (backend vs. frontend defaults) | Decision: expose presets at all, and which framing first — convenience/operability only, no new capability | ❌ Not implemented — no preset keywords found in `config.yaml` | JetBrains "Embark" |
| Index-freshness surfacing (B.8) | Attach "index freshness: Ns old / pending changes" note to `semantic_search` results | Decision: always-on vs. conditional (recommended: only when pending-changes > 0, to avoid token cost on the common case) | ❌ Not implemented — no freshness/pending-changes note in any `mcp-server/src/*.ts` tool response | JetBrains "Embark" |
| Local ONNX embedding fallback (B.9) | Remove the LM Studio/omlx HTTP dependency by embedding a local ONNX runtime model in-process | **Low urgency** — only worth it if the LM Studio dependency has caused real operational pain; would need to preserve the "always local" stance, not add a hosted fallback | ✅ Implemented (2026-09-11), differently than originally scoped — not literally in-process: a new shared `inference/` service (FastAPI + FastEmbed/onnxruntime, `nomic-embed-text-v1.5` for embeddings, ColBERT MaxSim for re-ranking) replaces the LM Studio/omlx HTTP dependency for embeddings *and* absorbs what was the separate reranker container, merged into one mandatory always-on container since re-ranking is no longer independently optional either. `watcher/embedder.py` and `mcp-server/src/embedder.ts` now call this internal service instead of LM Studio/omlx — still HTTP internally (container-to-container on the Docker network), not literally in-process, but the external LM Studio/omlx dependency for embeddings is gone. LLM (L1 summaries) is unchanged — still LM Studio/omlx/ollama. | JetBrains "Embark" |
| Vue `<script setup>` nested chunking (B.10) | Verify whether the `<script>` block inside Vue SFCs is already tree-sitter-boundary-chunked | **Now unblocked** (Tier‑1 shipped) — verification task, not a design decision; apply the same tree-sitter technique if not already sub-chunked | ⚠️ Verification confirms the gap is real — `_chunk_vue` (`chunker.py:983`) emits one opaque `<script>` block chunk; `_chunk_by_treesitter` is wired only for `.java`/`.cs`/`.ts`/`.tsx` file extensions (`chunker.py:910-953`), not invoked on the extracted Vue `<script>` sub-block. Still open, as predicted. | JetBrains "Embark" |
| Dispatch-table chunking threshold check (C.2) | Verify whether `case "get_index_status":`-style arms actually trigger the existing dispatch-arm sub-chunking pass | Likely a config/threshold tuning task, found via internal benchmark query 7 | ℹ️ Partially clarified, not resolved — dispatch sub-chunking (`_DISPATCH_TABLE`, `chunker.py:612-618`) already covers `.py` (`match`/`case`) with a 3-arm minimum gate (`_MIN_ARMS_FOR_SUBCHUNK`); whether *this specific* arm clears that gate still needs a direct check against the real file, not yet done | Internal benchmark |
| Published large-repo benchmark + 3-tier token-cost comparison (C.3) | Documentation/benchmarking effort: dump-full-file vs. LLM-summarized vs. `.ai-context`, at scale | No decision yet on priority — template borrowed from agentmemory (tokens/year + $/year table), scale framing from codebase-memory-mcp's published benchmark (83% answer quality, 10× fewer tokens, 2.1× fewer tool calls across 31 repos) | ❌ Not written — `docs/benchmark/benchmark-results.md` exists (3-way ai-context/idea-mcp/baseline comparison, real token counts) but no dump-full-file-vs-summarized-vs-ai-context 3-tier write-up or published/external version | agentmemory, codebase-memory-mcp |
| Session/decision-memory layer (C.4) | Cross-session "why did we do X" recall (decay/consolidation, session replay, multi-agent leasing) | **Explore only, not urgent** — different problem from L0/L1/L2 retrieval; only pursue as a separate future layer if multi-session recall becomes a real need | ❌ Not implemented — no `manage_adr`/`adr_records`/`search_history` anywhere in `mcp-server/src` or `watcher`; `decision-history-and-adr-plan.md` itself is still `Status: proposed` | agentmemory |
| Static `AGENTS.md`/Cursor-rules fallback file (C.7) | Periodically regenerated flat file at repo root with a condensed L0 map, for agents that don't do MCP tool orchestration | Low cost, low risk, purely additive alongside the existing MCP `instructions` field + skill file — no decision yet on priority | ⚠️ Partially exists in a different form — `AGENTS.md` at the repo root already exists (650 bytes) but is a static **routing table** (skill name + MCP server key per project), not the "condensed L0 map" C.7 envisioned. The zero-dependency-bootstrap goal is only half-covered: an agent without MCP access gets pointers, not actual structure/content. | vihaannarkhede29/contextos |
| Broader tree-sitter language coverage audit (C.8) | Audit which of the ~18 regex-fallback-chunked languages in `chunker.py` actually have a mature `tree-sitter-languages` grammar available | Needs a per-language audit before it's a real plan item, not just a claim to take at face value | ❌ Not audited — confirmed still open: `symbol_extractor.py` already has tree-sitter grammars wired for Python/TS/TSX/JS/JSX/Java/C#/Kotlin/Swift (used for the *symbol index*), but `chunker.py`'s *boundary chunking* (`_chunk_by_treesitter`) only uses tree-sitter for `.java`/`.cs`/`.ts`/`.tsx` — Kotlin/Python still hit `_chunk_by_pattern` regex fallback despite tree-sitter grammars already being available for them (see C.9 below, same root gap) | vinay02022/ContextOS |
| Kotlin/Python Tier‑2 tree-sitter chunking (C.9) | Extend `_chunk_by_treesitter`/`_collect_top_level` to Kotlin (extension functions, `when`, multi-line data classes) and Python (decorators, nested functions) | **Now unblocked** (Tier‑1 shipped) — likely a short follow-on plan reusing the existing mechanism | ❌ Confirmed not implemented — `chunker.py:910` (`.kt`) and `:933` (`.py`) both call `_chunk_by_pattern` directly, `_chunk_by_treesitter` is never attempted for either extension | JetBrains "Embark" |

**Recorded as rejected, do not re-open without new evidence:**
- **C.6 — Fully deterministic, embeddings-free retrieval as an alternate mode** (`vinay02022/ContextOS`'s approach). Rejected: pure graph traversal only finds what's connected by explicit typed edges, can't surface conceptually-related-but-structurally-disconnected code the way embedding similarity can, and contradicts `.ai-context`'s own multi-granularity retrieval rationale.

---

## 2. New finding: Serena (`oraios/serena`) — September 2026

Not previously tracked anywhere. Summary (full detail in the Sept 2026
competitive-landscape session; verified via live fetch of
`github.com/oraios/serena`):

- MCP server providing **symbol-level** code retrieval/editing/refactoring:
  `find_symbol`, `symbol_overview`, `find_referencing_symbols`,
  `find_declaration`, `find_implementations`, `replace_symbol_body`,
  `insert_after/before_symbol`, `safe_delete`, `rename`,
  `search_for_pattern`, plus a cross-session memory system.
- Two backends: open-source **LSP** (40+ languages) or a **paid JetBrains
  plugin** backend (adds type hierarchy, cross-dependency search,
  move/inline refactoring, full debugger integration).
- Install: single `uv tool install serena-agent` — **no Docker, no
  Postgres, no vector DB**.
- Fully local, actively developed (references to Claude Opus 4.6/GPT 5.4 in
  current docs).

**Critical gap vs. `.ai-context`:** Serena has **no embeddings, no vector
search, no L1 summaries, no re-ranking, no filesystem watcher, no delta
re-indexing**. It cannot answer "find code semantically similar to X" when
the identifier is unknown — only regex/symbol-name lookup. Conversely, its
symbol-editing tools (rename across files, safe delete, move symbol) go
**beyond** what `.ai-context`'s read-only `resolve_symbol`/`analyze_impact`
tools do today.

**Verdict:** highest-priority tool to monitor. It substantially covers the
LSP-bridge + MCP-symbol-tool layer with a much lighter footprint (single
process vs. `.ai-context`'s Docker Compose stack), but is complementary, not
substitutive, for the L1/L2 retrieval layers that are `.ai-context`'s actual
differentiator.

---

## 3. Roadmap — sequenced, with benefit vs. current solution

### Phase 0 — Hygiene (near-zero effort, do first)

| Item | Change | Status (2026-09-01) | Benefit vs. current |
|---|---|---|---|
| Update OpenCode → Crush references | `config.yaml`, `README.md`, `remote-repo-server-plan.md` still say "OpenCode" (`opencode-ai/opencode` is archived; confirmed via direct fetch — project continues as `charmbracelet/crush`, same author/team) | ✅ `remote-repo-server-plan.md` fixed (2026-09-03). `config.yaml`/`README.md` were already fixed in an earlier accuracy-audit pass this repo went through. | Removes stale/broken references; MCP integration is unaffected since Crush supports MCP the same way |
| Formally record Serena/claude-context/Aider verdicts | Add §2 above (done) instead of leaving this only in chat history | ✅ Done — this document | Prevents re-discovering the same findings from scratch next time; keeps the "why we still need this stack" argument evidence-based |
| Mark `decision-log-combined.md` superseded | One-line header note pointing here | ✅ Done | Stops the stale doc from being mistaken for current guidance |

> **Correction (2026-09-14):** the "Update OpenCode → Crush references" row above conflated two distinct projects. `opencode-ai/opencode` is indeed archived and Crush (`charmbracelet/crush`) is its successor — that part still stands. But **`sst/opencode`** is a *separate*, actively maintained project that also carries the "OpenCode" name and was never archived; it is not the same lineage as Crush. It has a materially richer native extension model than plain MCP consumption (JS/TS plugin API, custom tools, lifecycle hooks including a `config` hook and `file.watcher.updated`/`file.edited` events, native skill discovery, and built-in LSP integration for ~25 languages). `README.md` has been updated (2026-09-14) to disambiguate the two rather than treat "Crush (formerly OpenCode)" as the complete picture. See the top-level `opencode-ai-context-plugin/` package (in progress) for the `sst/opencode`-targeted native plugin this distinction motivated.

### Phase 1 — Quick verification/decision items (low effort)

| Item | Benefit vs. current |
|---|---|
| Vue `<script setup>` nested chunking (B.10) — now unblocked | Either confirms an existing capability (no-op) or closes a real blind spot in Vue SFC search with the mechanism you already have |
| Dispatch-table chunking threshold check (C.2) | Likely a config tune, not new code — closes a benchmark-observed blind spot cheaply |
| Nested `.gitignore` decision (B.3) | Just a decision — either closes a documented monorepo edge case or is explicitly dropped so it stops recurring as a "maybe" |

### Phase 2 — Concrete retrieval-quality gains (medium effort, now unblocked)

| Item | Benefit vs. current |
|---|---|
| React hook sub-chunking (A.5) | Makes values computed inside `useEffect`/`useMemo`/custom hooks searchable — currently invisible to `semantic_search` inside a component chunk |
| Kotlin/Python Tier‑2 tree-sitter chunking (C.9) | Replaces regex-fallback boundary detection with real AST boundaries for two more Tier‑1-adjacent languages — same precision gain Tier‑1 already delivered for TS/Java/C# |
| Broader tree-sitter coverage audit (C.8) | Turns an unverified claim ("~18 languages on regex fallback") into a concrete list of what can move to the primary tree-sitter path |
| Richer L0 module info from `symbol_index`/`call_edges` | Derive "entry point" generically as *"a function with zero incoming `call_edges` rows"* — one SQL query, works for Python/Go/Rust/C# with zero new per-language parser code, instead of today's hand-rolled per-language `l0_generator.py` parsers. Originally scoped in `structured-profile-plan.md` §3 as explicit follow-on work, parked here now that doc has been folded into `docs/configuration.md` |

### Phase 3 — Needs measurement/experiment before committing

| Item | Benefit vs. current |
|---|---|
| Parallelize `run_full_index()` (B.4) | Only worth it if per-stage timing shows the embed/summarize network calls (not chunking) dominate and the local model server doesn't already serialize concurrent requests — measure before building |
| RRF experiment (B.2) | Only adopt if a real benchmark run against a query set shows it beats the current single-channel + keyword-boost design — avoids adopting a technique on "other projects use it" alone |

### Phase 4 — Operability/convenience (no new retrieval capability)

| Item | Benefit vs. current |
|---|---|
| Retrieval-quality presets (B.7) | Lets teams tune quality/perf per project without code changes — convenience only |
| Index-freshness surfacing (B.8) | Reduces confusing agent behavior right after an edit, at near-zero token cost if conditional |
| Backup/restore hardening (B.1) | Reduces git-history bloat / merge-conflict risk on committed snapshots |

### Phase 5 — Low priority / explore only

| Item | Benefit vs. current |
|---|---|
| Session/decision-memory layer (C.4) | Different problem (cross-session recall) — only build if that need actually shows up |
| Static `AGENTS.md` fallback file (C.7) | Zero-dependency bootstrap for non-MCP agents — purely additive, low priority |
| Published large-repo benchmark write-up (C.3) | Strengthens the "why not just grep/idea-mcp" argument with the token-cost numbers already measured in `benchmark-results.md` — documentation effort, not code |

### Standalone large-scope plans (own files, not folded into the phases above)

Two `next-plans/*.md` files describe substantial, not-yet-implemented
features that don't map onto any single decision-log item above — each
has its own full design doc and stays there rather than being compressed
into a table row. Listed here (2026-09-03 audit) purely so they aren't
silently lost the way `decision-log-combined.md`'s stale items were —
confirmed via direct code search, no implementation exists for either:

| Plan | What it proposes | Status | Priority |
|---|---|---|---|
| [`external-docs-and-dependency-surface-plan.md`](external-docs-and-dependency-surface-plan.md) | Index external language/library docs + the project's own direct dependencies' public API surface, reusing the existing chunker/Qdrant/Postgres pipeline | ❌ Not implemented — `Status: proposed` in the doc itself; no crawler/dependency-surface code anywhere in `watcher/` or `mcp-server/src/` | Not yet prioritized — genuinely useful (closes the "stale training-data recall for library APIs" gap) but depends on the `hover`/definition data documented in `docs/lsp-integration.md` for Pillar 3, which is available now that LSP is shipped |
| [`remote-repo-server-plan.md`](remote-repo-server-plan.md) | Turn `.ai-context` from a local-only tool into a server-hosted, multi-repo, webhook-driven service with a web frontend | ❌ Not implemented — no webhook/git-sync/clone code anywhere in the codebase | Large architectural pivot (local tool → hosted service), not a gap-fix on the current design; only worth pursuing if there's an actual product decision to offer `.ai-context` as a hosted/team service rather than a per-developer local stack |

### Not on the roadmap (rejected/out of scope)

- **C.6** deterministic embeddings-free retrieval — rejected, see §1.
- **Serena-style symbol-editing tools** (rename/move/safe-delete) — `.ai-context` is a read/analysis context layer, not an editing tool; adopting Serena's editing scope would be a deliberate scope expansion, not a gap fix. Recommend explicitly staying out of that lane rather than competing with Serena/JetBrains PSI on refactoring, and treating Serena as complementary tooling an agent can reach for alongside `.ai-context`, not a gap to close.

---

## 4. What this achieves overall, vs. the current solution

- **Closes real, already-identified blind spots** (Phases 1–2) using
  mechanisms that already exist (tree-sitter boundary chunking, benchmark
  infrastructure) — no new architecture, incremental precision gains on
  top of the current L0/L1/L2 stack.
- **Avoids two premature architecture changes** (RRF, parallel indexing) by
  gating them on measurement/experiment first, per §1 — prevents spending
  effort on a change that "other projects do" but hasn't been shown to beat
  the current single-channel design here.
- **Keeps the LSP-bridge/editing scope boundary explicit** against Serena,
  rather than drifting into refactoring-tool territory where a lighter,
  more actively developed competitor already leads.
- **Prevents the stale-decision-log failure mode from recurring**: every
  item above now has either a dedicated plan file (already existed) or a
  home in this roadmap (new) — nothing is tracked *only* in chat history or
  in a document nobody updates anymore.
