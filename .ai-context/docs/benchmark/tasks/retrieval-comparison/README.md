# Retrieval-comparison task pack

This is the migrated (not a replacement — see below) version of the old
`.github/skills/benchmark` 3-arm retrieval-tool-comparison benchmark,
ported onto the `.ai-benchmark/` framework (see
`.ai-context/docs/next-plans/ai-benchmark-plan.md` §6 for the full
rationale). It lives here, **outside** `.ai-benchmark/`, because it's
this repo's own Task definition — `.ai-benchmark/` itself stays free of
any `ai-context`-specific assumptions so it can be copied into other
repos as-is (see `.ai-benchmark/README.md`).

**The old skill and its files (`.github/skills/benchmark/**`) are kept,
unmodified, alongside this** — this task pack is an additive alternative
execution path, not a replacement. Either can be used to run a benchmark
arm; the old skill drives an agent turn-by-turn through the prose
protocol docs, this task pack is the machine-runnable counterpart to the
same query set/arms.

## What's here

| File | What it is |
|---|---|
| `dataset.json` | (Not published — targeted a private codebase; provide your own.) All 30 queries + ground truth, extracted **programmatically** (not hand-transcribed) from a private query-set file — verified byte-for-byte reconcilable against that source. |
| `arms/ai-context.yaml` | Tool allowlist for the `ai-context` arm, extracted from a private query-set file (the `protocolDoc` field points back at it — that .md remains the authoritative full protocol). |
| `arms/idea-mcp.yaml` | Same, for the `idea-mcp` arm / `benchmark-session-idea-mcp.md`. |
| `arms/baseline.yaml` | Same, for the `baseline` arm / `benchmark-session-baseline.md`. |
| `task.yaml` | Ties the above together + documents the runbook (see below). |

## Running a session

Each arm still needs a **brand-new session/context that has not yet
explored the target repo** — that isolation is the entire point, same
requirement as the old skill. Use `.ai-benchmark/bin/cli.js` as the
instrumentation CLI (bench.js's direct successor — same wrap/start/stop/
report shape, but persists into `.ai-benchmark`'s Run lifecycle instead of
a flat log file):

```bash
cd .ai-context/docs/benchmark/tasks/retrieval-comparison
CLI=../../../../../.ai-benchmark/bin/cli.js
SESSIONS=../../sessions   # same output location the old skill used — commit this

# MODE 1 — wrap a single shell command (arm=ai-context, query=q1):
node "$CLI" wrap --arm ai-context --query q1 --tool grep --sessions-dir "$SESSIONS" -- <command...>

# MODE 2 — start/stop around an MCP/IDE tool call:
node "$CLI" start --arm ai-context --query q2 --tool semantic_search --input "..." --sessions-dir "$SESSIONS"
# ...perform the actual tool call through your own mechanism...
node "$CLI" stop --id <call_id> --output "..."

# MODE 3 — aggregate + finalize into a Report once an arm is done:
node "$CLI" report --arm ai-context --csv ai-context_summary.csv --sessions-dir "$SESSIONS" --finish
```

`report --finish` finalizes the Run and writes `report.json`/`report.md`
into `sessions/<runId>/` — commit those alongside the CSV, same as the old
skill's `benchmark-session-*.md` + `*_summary.csv` outputs.

⚠️ **Point `--sessions-dir` at a scratch directory while testing the
pipeline itself** (e.g. `/tmp/...`) — only point it at the real
`../../sessions` once you're recording an actual benchmark run you intend
to keep. Don't `rm -rf` that directory to "reset" it; it holds real,
committed historical results.

## Validation status

Structural/operational validation (plan §6.1 — this does **not** claim
numeric equivalence with old `bench.js`-era results, which is explicitly
not a meaningful comparison — see the plan for why):

- ✅ Dataset is byte-for-byte reconcilable with the source `.md` (all 30 rows, 0 mismatches).
- ✅ Arm configs map 1:1 onto their source protocol docs.
- ✅ A fresh 3-arm operational smoke test (simulated tool calls) completed with 0 incomplete sample/arms and the expected Report shape.

A real, full 3-arm live run against the actual target repo (for real
comparative numbers) has **not** been done yet via this task pack — that's
a separate, substantial exercise an agent/operator would run interactively
per arm, same as with the old skill.
