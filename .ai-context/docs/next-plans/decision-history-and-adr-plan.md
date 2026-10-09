# Decision History: ADR Tool + `search_history` + Commit-Message Mining

Status: proposed
Depends on: nothing (additive); reuses the existing Postgres/MCP-tool
dispatch pattern unchanged
Combines three related, previously-separate decision-log items into one
plan, since they share a data source (git history / "why" questions) and
were flagged in `decision-log-combined.md` as complementary, not
independent:

- **A.2** — Lightweight ADR/decision-record tool (`codebase-memory-mcp`'s
  `manage_adr`)
- **C.1** — `search_history` tool (git blame/commit/file-history search,
  from `code-memory`)
- **C.5** — Commit-message-derived decision mining (from
  `vihaannarkhede29/contextos`)

## 0. Problem statement

Today `.ai-context` answers "what does this code do" and "what would
break if I change it" (`semantic_search`, `get_file`, `analyze_impact`)
well, but has zero coverage of "why was this built this way" or "who/when
did this change and what did the commit message say". Both are common,
real questions a coding agent gets asked, and the only current source of
truth for either is training-data recall (stale) or the developer's own
memory (not queryable at all).

This plan closes that gap with three pieces, deliberately ordered from
cheapest/most-standalone to most-dependent:

1. **ADR tool (A.2)** — a CRUD tool for explicit, human-authored decision
   records. Zero external dependency, doesn't need git at all.
2. **`search_history` tool (C.1)** — live git log/blame/file-history
   queries, on-demand (not pre-indexed), similar in shape to the already-
   shipped `resolve_symbol`'s live-LSP-call pattern rather than
   `semantic_search`'s pre-indexed-vector pattern.
3. **Commit-message decision mining (C.5)** — a periodic batch job that
   scans git log for decision-bearing commits and turns them into
   *suggested* ADR entries (never auto-committed as fact) — the one piece
   that meaningfully depends on both 1 and 2 existing first.

## 1. ADR tool (A.2)

### 1.1 Why this shape, not a free-text notes tool

`codebase-memory-mcp`'s `manage_adr` and the classic Michael Nygard ADR
template both converge on the same small, structured shape: a decision
needs a title, a status (proposed/accepted/superseded/rejected), the
context that motivated it, the decision itself, and consequences. A
free-text notes tool would be cheaper to build but loses exactly the
structure that makes ADRs skimmable and supersedable — worth the small
extra schema cost.

### 1.2 Schema

```sql
CREATE TABLE IF NOT EXISTS adr_records (
  id            SERIAL PRIMARY KEY,
  project_key   TEXT NOT NULL,
  title         TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'proposed',  -- proposed | accepted | superseded | rejected
  context       TEXT,
  decision      TEXT NOT NULL,
  consequences  TEXT,
  superseded_by INT REFERENCES adr_records(id),
  source        TEXT NOT NULL DEFAULT 'manual',     -- manual | commit_mining (see §3)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_adr_project ON adr_records(project_key);
```

Follows the existing `infrastructure/init.sql` convention (`CREATE TABLE
IF NOT EXISTS`, `project_key` as the multi-project scoping column, same
style as `import_edges`/`call_edges`). Add alongside those in `init.sql`
and add matching CRUD helpers to `watcher/db.py` (`upsert_adr`,
`get_adr`, `list_adrs`, `supersede_adr`) — same pattern as the existing
`upsert_call_edges`/`delete_call_edges` pair.

### 1.3 MCP tool: `manage_adr`

Single tool, action-dispatched (mirrors how `resolve_symbol` dispatches on
`kind`), not four separate tools — keeps the agent-facing tool count
growth to +1, not +4:

```
manage_adr(action, [id], [title], [status], [context], [decision], [consequences], [supersedes])
```

- `action: "create"` — requires `title`, `decision`; `context`/
  `consequences` optional. Returns the new `id`.
- `action: "list"` — optional `status` filter; returns title/status/id/
  created_at for all matching records, newest first.
- `action: "get"` — requires `id`; returns the full record.
- `action: "update"` — requires `id`; updates any provided field,
  `updated_at` bumped.
- `action: "supersede"` — requires `id` and `supersedes` (the old
  record's id); sets old record's `status = 'superseded'` and
  `superseded_by = <new id>` atomically.

Reuses `semantic_search`'s existing indexing path for *retrieval*, not a
new lookup mechanism: ADR content is also written as a synthetic Markdown
file (`<adr-cache>/<project>/<id>-<slug>.md`, same materialize-then-index
principle as `external-docs-and-dependency-surface-plan.md`) so an ADR
surfaces in ordinary `semantic_search` results too, tagged
`source_type: adr` — not just via explicit `manage_adr` calls. This
avoids needing a second retrieval path for something that's fundamentally
just more indexed prose.

### 1.4 Status: ready to scope

No open decisions blocking this — matches `decision-log-combined.md`'s
own "ready to scope" status for A.2.

## 2. `search_history` tool (C.1)

### 2.1 Shape: live query, not pre-indexed

**Confirmed via the codebase**: neither the watcher (`Dockerfile`) nor the
mcp-server (`Dockerfile`) has the `git` binary installed today, and no
git-log/blame parsing exists anywhere in the repo. Two implementation
shapes were considered:

- **Pre-indexed** (code-memory's approach: BM25+vector over commit
  history) — would need a new indexing pass, new storage, and staleness
  tracking (commits keep happening). Heavier, and duplicates
  infrastructure `semantic_search` already has for a fundamentally
  different, much smaller corpus (commit messages, not source).
- **Live query** (this plan's recommendation) — shell out to `git log`/
  `git blame` on-demand, same pattern already proven by `resolve_symbol`
  calling out to the live LSP bridge instead of pre-indexing symbol
  tables a second time. Git itself is already a fast, well-indexed store
  for this exact class of query — no need to duplicate it.

**Recommendation: live query.** Simpler, no staleness problem (git *is*
the source of truth), and consistent with this session's general bias
toward reusing an existing authoritative source over pre-indexing a copy
of it (mirrors the `get_file`/`doc_trees` design principle: prefer a
direct, unranked read of a known-good source over a scored index when
the underlying data is already fast to query directly).

### 2.2 Where it runs

Recommend the **mcp-server** container (Node/TS, already handles all
tool dispatch and is the process fielding the MCP `resolve_symbol` call
today), not the watcher — the watcher's job is indexing, not live
per-request queries, and adding a live-query responsibility there would
blur that boundary. Concretely:

- Add `git` to `mcp-server/Dockerfile`'s runtime stage (`apk add --no-cache
  git`, small, alpine-native).
- Add a `child_process.execFile("git", [...], { cwd: project.root_path
  })` wrapper in a new `mcp-server/src/git_history.ts` module — no
  shelling through a full shell string (avoids injection risk entirely;
  `execFile` with an argv array never invokes a shell).
- `root_path` is already bind-mounted read-only into the mcp-server
  container's project workspace (same mount `get_file`/`repo_structure`
  already read from) — `.git` sits alongside it in the normal case, no
  new mount needed. **Caveat to verify during implementation**: shallow
  clones (`git clone --depth`) or worktree setups may have a missing or
  partial history; `search_history` should degrade to "no history
  available" rather than erroring in that case.

### 2.3 Tool shape

```
search_history(action, [file_path], [query], [limit])
```

- `action: "log"` — `git log --follow -- <file_path>`, formatted as
  `commit / author / date / subject` lines, capped at `limit` (default
  20, max 100).
- `action: "blame"` — `git blame -L <start>,<end> -- <file_path>` when a
  line range is given (reuse `get_file`'s existing line-range parameter
  convention), otherwise whole-file blame capped by output size the same
  way `get_file` caps `max_chars`.
- `action: "search"` — `git log --all --grep=<query> -i` across commit
  messages (not full diff content in v1 — diff-content search is a much
  bigger token/perf cost for a first cut and is deferred, matching this
  session's "don't build for problems you haven't measured" posture).

No BM25/vector fusion in v1 — `git log --grep` is already a reasonable
substring/regex match for the common "which commit touched X" question;
revisit only if real usage shows it's too weak.

### 2.4 Status: ready to scope, one open decision

- **Output size bounding** — git blame/log output on a large, old file
  can be large. Recommend the same `max_chars`-cap-plus-coverage-note
  pattern `get_file` already uses, rather than inventing a new
  truncation convention.

## 3. Commit-message decision mining (C.5)

### 3.1 Why this depends on §1 and §2

This is the one piece of the trio that's genuinely new batch-processing
logic (not just a live query or simple CRUD), and it's most useful once
both pieces above exist: it needs somewhere to write suggestions (§1's
`adr_records` table, `source: 'commit_mining'`) and its detection logic is
the same git-log walk `search_history` already knows how to do — building
it standalone first would mean re-deriving that walk twice.

### 3.2 Detection heuristic (deliberately simple, not ML)

Scan commit messages (not diffs — diff-content analysis is a much larger,
separate effort and out of scope here) for decision-bearing signal:

- Conventional-commit-style prefixes that imply a deliberate tradeoff:
  `refactor!:`, `BREAKING CHANGE:`, or body text containing phrases like
  "migrate to", "switch from", "replace X with Y", "decided to".
- Commit subject length above a threshold (short commits are rarely
  decision records; a genuinely explanatory commit message tends to be
  longer) combined with a multi-paragraph body — a cheap, no-ML proxy for
  "this commit explains itself," reused as-is from the intent already
  established in `vihaannarkhede29/contextos`'s (unproven, thin)
  implementation, kept here only as a heuristic starting point.

This is intentionally a **precision-over-recall, human-reviewed**
pipeline, not an automatic ADR generator — every match becomes a `status:
'proposed'`, `source: 'commit_mining'` draft row via §1's `manage_adr`
`create` path, never auto-promoted to `accepted`. A human (or the calling
agent, on request) reviews and promotes or discards each suggestion.

### 3.3 Where it runs

Follows the exact dispatch pattern `external-docs-and-dependency-surface-
plan.md` proposes for its own new one-shot mode (`MODE=external_docs`) —
confirmed via `watcher/entrypoint.py`'s existing `MODE` dispatch table
(`full`/`l0`/`watch`/`restore`/`backup`):

- New `MODE=commit_mining` one-shot mode, added to the same dispatch
  table, implemented as `watcher/commit_miner.py` (new file, following
  the `l0_generator.py`/`backup.py` module-per-mode convention already
  used there).
- Needs the `git` binary added to `watcher/Dockerfile` too (separate
  install from mcp-server's — different container, different base image
  concerns already noted in that Dockerfile's per-language conditional
  `RUN` blocks).
- Triggered the same way `external_docs`'s periodic re-check is proposed:
  not on every file-watch debounce cycle (git history barely changes
  commit-to-commit in a way that needs immediate reaction), but a
  lightweight periodic re-check (cron / CI schedule / manual
  `scripts/mine_commit_history.sh`) that only processes commits newer
  than the last-seen commit SHA (tracked in a new small table,
  `commit_mining_state(project_key PK, last_scanned_sha)`).

### 3.4 Status: needs a decision before planning further

- **Threshold tuning** — the "long commit message" heuristic in §3.2 has
  no principled default yet; needs a first pass against this repo's own
  git history (or a few of the comparison repos) to see what a reasonable
  length/keyword threshold actually looks like before committing to a
  number. Treat as an investigation step, not a design decision, same
  spirit as `decision-log-combined.md`'s B.4 (measure before deciding).
- **Opt-in or on-by-default?** Recommend opt-in per project
  (`commit_mining: { enabled: false }` in config, same "empty/false unless
  opted in" convention as `external_docs.enabled` and
  `index_dependency_surface`) — a wrong-heuristic false-positive flood of
  draft ADRs on first run would be a bad first impression; let teams turn
  it on once they've seen the ADR tool's shape from §1 first.

## 4. Tool-count impact

+2 agent-facing MCP tools (`manage_adr`, `search_history`) — takes the
tool surface from 5 to 7. Both are optional per-project capabilities in
spirit (an ADR-empty or git-shallow project just gets short/empty
responses, not errors), consistent with how `analyze_impact`/
`resolve_symbol` already degrade gracefully rather than requiring
universal applicability.

## 5. Phased rollout

- **Phase 1**: ADR tool (§1) — ships standalone, no dependency on
  anything else in this plan. Schema + `manage_adr` tool + Markdown
  materialize-and-index hook.
- **Phase 2**: `search_history` tool (§2) — ships standalone, no
  dependency on Phase 1. `git` binary in `mcp-server/Dockerfile` +
  `git_history.ts` + tool registration.
- **Phase 3**: Commit-message mining (§3) — depends on both Phase 1 (needs
  `adr_records` to write into) and Phase 2 (reuses the same git-log
  walk), plus the threshold-tuning investigation from §3.4. Ship last,
  opt-in, after the heuristic has been sanity-checked against real
  history.

## 6. Open decisions carried forward

1. **§2.4** — output-bounding convention for `search_history` (recommend
   reusing `get_file`'s `max_chars`/coverage-note pattern).
2. **§3.4** — commit-mining detection thresholds (needs a measurement
   pass first) and opt-in-vs-default (recommend opt-in).
3. **Shallow-clone/worktree degradation** (§2.2) — confirm the exact
   failure mode (`git log` on a shallow clone returns partial history,
   not an error) and make sure `search_history`'s response makes that
   limitation visible to the calling agent rather than silently looking
   like "no history exists."
