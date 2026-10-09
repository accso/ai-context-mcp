# Benchmark Session A — Toolset: `ai-context`

**Give this document to an agent in a brand-new session/context that has NOT
previously explored the target repository.** The agent needs this document
plus the frozen shared query-set file (the query-set file or whatever
version the operator names — see Step 1) — nothing else. It does not need
`benchmark-plan.md`, `benchmark-results.md`, or either of the other two session
documents (`benchmark-session-idea-mcp.md`, `benchmark-session-baseline.md`) to
run this session. Do not show the agent those other documents before or during
this session — that would defeat the isolation this benchmark depends on.

---

## When and how to use this document

Use this document **only** when you are the agent executing **one isolated arm**
of the benchmark. It must be handed to you in a **brand-new session** before you
have explored the repository.

You receive **this document + the frozen query-set file** and nothing else. Do
not ask for, open, or read the other two session documents, `benchmark-plan.md`,
or `benchmark-results.md`; doing so invalidates the isolation this benchmark
depends on.

Follow the steps in order:
1. Fill in the operator-supplied metadata.
2. Copy the query set verbatim into your report.
3. **Instrument every action with `bench.js`** (`.ai-context/docs/benchmark/bench.js`, relative to the repo root) using `--session ai-context`. This is mandatory: the benchmark compares real wall-clock time and real token counts across the three arms, so any action that is not logged by `bench.js` cannot be scored and is treated as if it never happened. Use `wrap` for single shell commands and `start`+`stop` for MCP tool calls. Do not estimate timing or tokens yourself — that is exactly what `bench.js` replaces.
4. Answer every query using **only** the `<project>-context_*` tools listed below.
5. Produce the final report and save it with the required filename.

If you are unsure whether a tool is allowed, treat it as **not allowed** and record
the gap in the Notes column instead of using it.

---

## For the Benchmark Operator — fill this in before handing the document to the agent

| Field | How to get it | Value |
|---|---|---|
| Target repository | — | |
| Date/time run | — | |
| Total files (approx.) | e.g. `find <repo_root> -type f \| wc -l` (exclude `.git/`, `node_modules/`, build output) | |
| Total folders (approx.) | e.g. `find <repo_root> -type d \| wc -l` | |
| Primary language(s) | eyeball the repo, or `.ai-context/config.yaml`'s `profile` field(s) | |
| Executing agent / LLM model | whatever model is running this benchmark session | |

Fill in the **Metadata** table at the top of your final report (Step 3
below) with these values.

---

## Your role & constraints (read carefully — this is the whole point of the session)

You are running **one isolated arm of a 3-arm benchmark** comparing
different ways of answering the same real questions about a codebase. Two
other arms exist (a different MCP toolset, and a no-special-tooling
baseline) — **you must not use, reference, or think about those**. Answer
every query using **only** the toolset below, exactly as a developer using
only this toolset would have to.

**Allowed tools — strictly the `<project>-context_*` MCP tools, nothing
else.** This benchmark's target is a multi-project monorepo, which is indexed as
**five separate `ai-context` projects**, each exposed as its own MCP server
with its own tool prefix. Use whichever project's tools match the query —
for a query about `project-a` code use the `project-a-context_*`
tools; for a cross-module query, call the tools on *each* project the query
spans (that's still "only this toolset," just invoked against more than one
of its five per-project instances):

- `project-a-context_repo_structure` / `_semantic_search` / `_get_file`
- `project-b-context_repo_structure` / `_semantic_search` / `_get_file`
- `project-c-context_repo_structure` / `_semantic_search` / `_get_file`
- `project-d-context_repo_structure` / `_semantic_search` / `_get_file`
- `ai-context-context_repo_structure` / `_semantic_search` / `_get_file`

Each behaves identically to the generic `repo_structure([dir_path],
[depth])` / `semantic_search(query)` / `get_file(file_path, [start_line],
[end_line])` signature described elsewhere in this document — only the
tool-name prefix changes depending on which of the five projects you're
querying.

**Not allowed:** any generic filesystem `read`/`grep`/`glob`/`ls` tool, any
IDE-native tool (e.g. JetBrains `search_symbol`/`search_text`), web search,
or anything else your environment might offer. If a tool call would use
something outside the fifteen tools above, don't make it — note in your
results that you *would* have needed a different tool, instead of reaching
for one.

**This restriction is the entire point of the session — enforce it
strictly on yourself.** A single off-toolset call (even "just to double
check" or "just to orient myself") invalidates that query's data point for
this arm of the benchmark. If you are unsure whether some tool your
environment happens to expose counts as one of the fifteen `<project>-context_*`
tools above, treat it as **not allowed** by default and record the gap in
your Notes column instead of using it. Do not use any tool — including
this same environment's own generic file/search tools — to shortcut a
query you're struggling with.

---

## Step 1 — Load the shared query set (do not generate your own)

All three arms of this benchmark (`ai-context`, `idea-mcp`, baseline) must
answer the **exact same queries** against the **exact same pre-written
ground truth**, otherwise their results aren't comparable and the "who
scored better on query N" comparison in the final analysis is meaningless.

**Do not invent your own queries and do not explore the repository to come
up with a query list.** That exploratory pass would itself require tools
outside the three you're restricted to, and it would make you both the
author and the grader of your own test — exactly the self-scoring problem
this redesigned benchmark exists to eliminate.

Ask the benchmark operator for the frozen query-set file for this run
(the query-set file they provide). It contains the fixed
list of queries (with categories) and the pre-written ground truth, prepared in
advance by someone other than the three session executors. If it doesn't exist
yet or isn't frozen/committed, stop and tell the operator it must be prepared
first — do not proceed to Step 2 without it.

Copy that query list verbatim into the **Query Set & Ground Truth** table
of your Step 3 report.

---

## Benchmark instrumentation (required for this session)

This session is part of a controlled benchmark comparing tool-retrieval approaches.
Every tool call/action you make while answering a benchmark query MUST be logged using
`bench.js`, a script at `.ai-context/docs/benchmark/bench.js` (relative to the repo
root). Do not skip this — uninstrumented calls cannot be scored and will be treated as
if they never happened.

**Before your first query**, use `ai-context` as the `--session` value for
every logging call in this session (do not use a different name — the other two arms of
this benchmark use their own values, e.g. `ai-context`/`idea-mcp`/`baseline`, and mixing
them would break the per-arm aggregation).

**Before each query**, note its ID (Q1, Q2, ... as given in the query set) — you will pass
it as `--query` on every logging call for that query, so calls can be grouped correctly.

### If the action is a single shell command (glob, grep, read, find, cat, ls, ...)

Prefix it with `wrap` instead of running it directly:

```
node .ai-context/docs/benchmark/bench.js wrap --session ai-context --query <QID> --tool "<short tool label>" -- <the actual command and its arguments>
```

Example:
```
node .ai-context/docs/benchmark/bench.js wrap --session baseline --query Q7 --tool grep -- grep -rn "applyPendingChanges" project-c/
```

The real command still runs and its real output is still printed to you — `wrap` only adds
timing/token logging around it. Never run the same action twice (once wrapped, once not) —
just replace the direct call with the wrapped one.

### If the action is NOT a single shell command (an MCP tool call, an IDE action via
idea-mcp, a semantic_search/get_file/search_symbol call, etc.)

Call `start` immediately before making the real call, with the exact query text or symbol
name you are about to send as `--input`:

```
node .ai-context/docs/benchmark/bench.js start --session ai-context --query <QID> --tool "<tool name>" --input "<what you are about to search/read>"
```

This prints a `call_id=...`. Remember it, then perform the actual tool call through your
normal mechanism. Immediately after you receive the result, call `stop` with that id and
the result you got back:

```
node .ai-context/docs/benchmark/bench.js stop --id <call_id> --output "<the result text>"
```

If the result is long, write it to a temp file first and use `--output-file <path>` instead
of `--output` to avoid shell quoting issues.

### Rules

1. Every tool call gets exactly one `wrap`, or exactly one matching `start`+`stop` pair.
   Never leave a `start` without a matching `stop`.
2. Use the query ID that is actually being answered, not the previous one — this is what
   allows later per-query aggregation.
3. Do not paraphrase or summarize inputs/outputs when passing them to `bench.js` — pass the
   real query text and the real result text so token counts are accurate.
4. Do not estimate or report your own timing or token counts in your final answer — that is
   exactly what this instrumentation replaces. If asked to report cost/time for a query,
   say it is tracked via `bench.js` and available in the log, not a self-estimate.
5. At the end of the session, run:
   ```
   node .ai-context/docs/benchmark/bench.js report --session ai-context --csv ai-context_summary.csv
   ```
   and include the printed report in your session write-up.

---

## Step 2 — Execute each query using ONLY the toolset above (this is what gets measured)

For every query, answer it using only the `<project>-context_repo_structure`
/ `<project>-context_semantic_search` / `<project>-context_get_file` tools
for whichever of the five projects (`project-a`, `project-b`,
`project-c`, `project-d`, `ai-context`) the query concerns — using
more than one project's tools for a cross-module query is expected and
still within the allowed toolset — and record these five things:

1. **Tool calls (count)** — number of distinct tool invocations you made to
   reach your final answer for this query. List them in call order with
   their arguments.
2. **Tokens (estimate)** — approximate total tokens consumed by this
   query's tool interactions: your call arguments plus the raw text you
   got back. If you can't see an exact token count, estimate via
   `characters ÷ 4` and say so.
3. **Token quality (1–5)** — of what came back, how much was actually
   relevant/necessary to answer the query, versus noise you had to mentally
   filter past? `5` = nearly everything returned was directly useful,
   `1` = mostly irrelevant, you had to dig or guess.
4. **Time to return (seconds)** — wall-clock time from your first tool call
   for this query to having your final answer ready. If you can't observe
   real wall-clock time, estimate from call count/size and say so.
5. **Response quality** — compare your final answer against the ground
   truth from Step 1: `Exact` (right file, right location) / `Partial`
   (right file, wrong location, or needed a follow-up you didn't take) /
   `Miss` (wrong or no answer) / `Correct negative` (for negative-control
   queries, you correctly reported not-found). Add a one-line justification.

Don't backfill or "clean up" an answer after the fact using knowledge you
picked up from a later query — score each query on what you actually knew
at the time you answered it.

---

## Step 3 — Report

Produce a single Markdown report with exactly this structure:

```markdown
# Benchmark Session A — ai-context — <target repo name>

## Metadata
| Field | Value |
|---|---|
| Target repository | ... |
| Date run | ... |
| Total files (approx.) | ... |
| Total folders (approx.) | ... |
| Primary language(s) | ... |
| Executing agent / LLM model | ... |
| Toolset under test | ai-context (`<project>-context_repo_structure/semantic_search/get_file` across project-a, project-b, project-c, project-d, ai-context) |

## Query Set & Ground Truth
| # | Category | Query | Ground truth |
|---|---|---|---|
| 1 | ... | ... | ... |
...

## Results
| # | Tool calls (count + list) | Tokens (est.) | Token quality (1–5) | Time (s) | Response quality | Notes |
|---|---|---|---|---|---|---|
| 1 | ... | ... | ... | ... | ... | ... |
...

## Summary
- Total / average tool calls per query: ...
- Total / average tokens per query: ...
- Average token quality: ...
- Average time per query: ...
- Correctness breakdown: N exact / N partial / N miss / N correct-negative
- Qualitative observations (patterns across categories, anything that
  surprised you, anything that felt like a real gap vs. a one-off): ...
```

Save the report as `benchmark-session-A-ai-context-<repo-name>-<date>.md`.
