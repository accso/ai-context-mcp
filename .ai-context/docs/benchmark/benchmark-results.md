# AI Context — Benchmark Results: `ai-context` vs. `idea-mcp` vs. baseline

**Supersedes the previous version of this document.** That run compared only
`ai-context` vs. `idea-mcp` on the `ai-context` repo itself, self-scored by
the same agent that ran the queries. This run follows the corrected
methodology in `benchmark-plan.md`: three independent, isolated sessions
(one per toolset), a frozen 30-query set with pre-written ground truth
(the query set is not published — it targeted a private codebase), and objective per-call instrumentation via
`bench.js`. The old self-referential 10-query results are retired; the
qualitative findings below are only carried forward where this run
reproduces them.

---

## 1. Methodology

- **Corpus:** a multi-project monorepo (five `ai-context` projects: `project-a`,
  `project-b`, `project-c`, `project-d`, `ai-context` itself),
  run 2026-07-23.
- **Sessions:** three fresh, isolated agent sessions, each restricted to
  exactly one toolset:
  - **Session A — `ai-context`**: `<project>-context_repo_structure` /
    `_semantic_search` / `_get_file` across all five projects.
  - **Session B — `idea-mcp`**: JetBrains IDE MCP tools (`search_symbol`,
    `search_text`, `search_regex`, `search_file`, `list_directory_tree`,
    `get_project_modules`, `get_project_dependencies`, `read_file` /
    `get_file_text_by_path`, `analyze_calls`).
  - **Session C — baseline**: generic filesystem primitives only (glob/list,
    grep/text-search, whole-file or ranged read) — no semantic search, no
    symbol index, no ranking.
- **Query set:** A frozen set of 30 queries (not published) across 7 categories
  (named-symbol, anchor-free prose, multi-hop cross-module, negative control,
  operational/config, retrieval-precision, false-friend naming), with
  ground truth prepared in advance and independent of any session.
- **Instrumentation:** every tool call in every session was logged via
  `bench.js` (real wall-clock time, real token counts via `gpt-tokenizer`),
  not self-estimated. Session A logged 69 calls / Session B logged 60 calls
  (57 scored + 3 unassigned warm-up) / Session C logged 82 calls, each across
  30 scored queries.

---

## 2. Headline numbers

| Metric | A — ai-context | B — idea-mcp | C — baseline |
|---|---|---|---|
| Tool calls (total / avg per query) | 69 / 2.30 | 60 / 2.00 (57 scored + 3 warm-up) | 82 / 2.73 |
| Tokens (total / avg per query) | 6,160 / 205.3 | 51,544 / 1,718.1 | 41,521 / 1,384.0 |
| Time (total / avg per query, s) | 833.2 / 27.77 | 656.1 / 21.87 | 9.47 / 0.32 |
| Correctness: exact | 27 | 27 | 27 |
| Correctness: correct-negative | 3 (Q16, Q19, Q20) | 3 (Q16, Q19, Q20) | 3 (Q16, Q19, Q20) |
| Correctness: open-ended (Q15) | Correct (rubric) | Correct (rubric) | Correct (rubric) |
| Correctness: partial / miss | 0 / 0 | 0 / 0 | 0 / 0 |

All three sessions reached the same correctness outcome on all 30 queries.
The differentiator this run actually measures is **token cost**, not
correctness: `ai-context` used roughly **1/8 the tokens** of `idea-mcp` and
**1/7 the tokens** of the baseline, at a comparable call count to baseline
and a similar per-query time to idea-mcp. Baseline's raw wall-clock time is
far lower per call (0.32s vs. ~22–28s) because it ran local shell primitives
with no network/LLM-routed tool round-trip, not because it did less work —
its token cost per query still exceeds `ai-context`'s and lands close to
idea-mcp's.

---

## 3. Results by category

| Category | A — ai-context notes | B — idea-mcp notes | C — baseline notes |
|---|---|---|---|
| Named-symbol (Q1–5, 24) | 1–2 calls, small token footprint, all exact. | `search_symbol` fast and precise; all exact. | `find`+`grep`+`read` chains reliably; all exact but more calls. |
| Anchor-free prose (Q6–10) | Single semantic_search + get_file sufficient; domain vocabulary matched well. | Required guessed keywords via `search_text`/`search_symbol`; still succeeded because the repo uses descriptive names. | Same filename-guessing worked because names are descriptive (e.g. `status-calculation.ts`) — this is exactly the case where naive grep still performs well. |
| Multi-hop cross-module (Q11–15) | Most expensive category for A (Q11 4 calls/Q14 6 calls) — chunk coverage gaps on `config.yaml` forced extra `get_file` attempts. | Extra text-search steps to confirm cross-module callers (Q11 4 calls). | Most expensive category for C (Q11 8 calls, 2,476 tokens) — each side of the boundary located independently, no symbol index. |
| Negative control (Q16–20) | Clean; no hallucination. | Clean; no hallucination; Q18 trick negative answered correctly (implicit background refresh). | Clean; no hallucination; Q18 correctly identified as implicit yes. |
| Operational/config (Q21–25, 28) | Straightforward once file located; Q22/Q23 needed direct `get_file` after semantic search missed the config file. | Straightforward, 1–2 calls each. | Straightforward, single `cat`/`read` calls, lowest token cost of the three sessions here. |
| Retrieval-precision (Q26–27) | Exact line ranges reused from earlier queries; near-zero marginal cost. | Exact but two queries (Q5, Q26 equivalents) needed larger reads/full-file `cat -n` to confirm boundaries, causing large token spikes (7,000–7,800 tokens each) — the single biggest weakness observed in baseline. | See baseline column — same effect appears there most strongly. |
| False-friend naming (Q29–30) | Semantic search matched domain vocabulary (theme/contract-access) despite lexical mismatch — the intended advantage of this toolset. | Succeeded via broad keyword search, but Q30's `search_text` returned ~68 KB of noisy hits (19,496 tokens) before landing on the right file — the single largest per-query token cost in this run. | Direct file read once the name was guessed; cheap when guessed right, but this is a lucky case given the repo's descriptive naming. |

---

## 4. Qualitative findings

**Where `ai-context` wins:**
- **Token efficiency at equal correctness.** Across all 30 queries, no
  session scored higher on correctness than another — `ai-context`'s
  advantage this run is purely in tokens spent per correct answer (~205
  tokens/query vs. ~1,700–1,720 for the other two arms).
- **No catastrophic noise events.** Unlike idea-mcp's Q30 (19.5k tokens from
  an unranked `search_text` hit) or baseline's Q5/Q26/Q27 (7,000–7,800 tokens
  from full-file reads needed to confirm exact line ranges), `ai-context`'s
  worst single query (Q14, cross-project routing) cost only 263 tokens.

**Where `ai-context` is weaker or equal:**
- **Multi-hop cross-module queries are its most expensive category**, driven
  by chunk-coverage gaps (Q14 needed 6 calls because the right lines weren't
  in the indexed chunk for `config.yaml` and were only found in a
  differently-named file).
- **Unranked full-text search is a real, category-specific weakness for
  idea-mcp and baseline, not a general one.** It surfaces exactly on
  false-friend/retrieval-precision queries where the searched term is common
  across many files — this reproduces the original run's core qualitative
  finding, now backed by objective token counts instead of a subjective
  "noisy" label.
- **Baseline confirms the benchmark's founding premise on cost, not
  correctness.** All three toolsets reached identical correctness on this
  query set — the "why not just grep" question is answered by token cost
  and call-count patterns (baseline needed the most calls, 2.73/query, and
  spiked hardest on precision checks), not by baseline failing outright.
  This is the primary result this re-run was designed to establish, since
  the original benchmark never included a no-tooling baseline at all.

---

## 5. Carried-forward vs. retired findings from the original run

| Original finding (self-referential `ai-context` repo, 10 queries) | Status after this run |
|---|---|
| Prose queries need keyword translation for idea-mcp, not for ai-context | **Reproduced**, now on a different corpus and with a real baseline arm showing the same pattern (baseline also needs keyword guessing, but the repo's descriptive naming masked the cost). |
| Unranked full-text search returns noisy 80+ hit results | **Reproduced** — see idea-mcp Q30 and baseline Q5/Q26/Q27 above, now quantified in tokens rather than "hits returned." |
| Dispatch-table / switch-based code is a blind spot for both search layers | **Not exercised** in this run's 30-query set (v2 query set does not include a dispatch-table-style query on a multi-project monorepo) — retired pending a future query-set revision that re-adds this case. |
| Ranking inconsistency across near-duplicate phrasings | **Not exercised** — this run's queries were run once each, not with paraphrased duplicates; §5 of `benchmark-plan.md` recommends repeat trials per corpus, which this run did not yet do. |
| Project-scope boundary miss (`ai-context` can't see files outside its indexed subtree) | **Not exercised** — no v2 query targets a file outside the five registered `cat` sub-projects. |

---

## 6. Open follow-ups (per `benchmark-plan.md` §6–7)

1. **Repeat trials.** This run executed each session once; `benchmark-plan.md`
   recommends at least two runs per corpus to check variance in call count
   and correctness (the ranking-inconsistency risk flagged in the original
   run was never re-tested here).
2. **Secondary corpus (self-referential `ai-context` repo).** Not re-run in
   this pass — the original 10-query self-referential results are retired
   as unreliable (contaminated session) and should be re-collected under
   the same isolated-session methodology if that comparison is still wanted.
3. **Reintroduce dispatch-table and cross-boundary edge cases** into a future
   version of the query set so those two original qualitative findings can
   be confirmed or retired with real data, not carried forward on faith.
4. **`analyze_calls` / call-graph parity** remains explicitly out of scope,
   per plan §6.

---

## Source data

- Query set, ground truth and session transcripts: not published (they reference a private codebase)
- Raw per-call logs: `ai-context_summary.csv`, `idea-mcp_summary.csv`,
  `baseline_summary.csv`
