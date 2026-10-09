---
apply: always
---

# Test-driven development for `.ai-context` changes

Scope: any change under `.ai-context/` (excluding `.ai-monitoring` and
`.ai-benchmark` — separate tools moving to their own repos). Assumes the
three-tier test suite described in `.ai-context/docs/testing.md` — read
that file first if you haven't already; this file is the workflow
discipline for using it, not a duplicate reference.

## 1. Red → green → refactor, not "write code then maybe test"

For any behavior change (bug fix, new capability, refactor with an
observable effect):

1. **Red** — before or immediately alongside the implementation, write (or
   update) the test that captures the intended behavior. If fixing a bug,
   first write a test that reproduces it and confirm it actually fails
   against the current code — don't skip this step. A test never observed
   failing gives no real confidence it tests anything at all.
2. **Green** — implement the minimal change needed to make that test pass.
3. **Refactor** — clean up with the test suite as your safety net; re-run
   after every refactor step, not only at the end.

Exception: pure documentation/comment changes need no test. A config-only
change (e.g. a new `config.yaml` default) still needs one if any code
branches on the new value.

## 2. Pick the tier the change actually touches

Same mapping as `docs/testing.md` — add the test at the **lowest** tier
that can genuinely exercise the behavior (unit is cheaper, faster, and
localizes failures more precisely than integration or e2e):

| Change touches | Tier | Test lives in |
|---|---|---|
| Pure logic — no DB/network (`chunker.py`, `symbol_extractor.py`, `import_extractor.py`, `validate_config.py`, `context_assembler.ts`, `range_slice.ts`) | **Unit** | `<component>/tests/unit/` or `mcp-server/test/unit/` |
| Postgres/Qdrant-touching logic (`analyze_impact`, `indexer.py` delta-upsert/self-heal, `dependency_restore.py`, reranker backend selection) | **Integration** | `<component>/tests/integration/` or `mcp-server/test/integration/` |
| Cross-component behavior (indexing → MCP tool response, backup/restore, LSP bridge routing) | **E2E** | `mcp-server/test/e2e/` |
| Genuinely unsure, or touches more than one layer | Add tests at each layer actually touched; run `all` | — |

## 3. Run the matching tier before calling the change done

```bash
.ai-context/scripts/test.sh unit [component]         # after any pure-logic change
.ai-context/scripts/test.sh integration [component]  # after any Postgres/Qdrant-touching change
.ai-context/scripts/test.sh e2e [component]          # after any cross-component change
.ai-context/scripts/test.sh all                      # when unsure, or before finishing a larger change
```

A change is not done until:
- the new/updated test failed against the pre-change code (red), **and**
- it passes against the post-change code (green), **and**
- the **full matching tier** — not just the one new test — still passes.
  A regression surfacing in a neighboring test is exactly what running the
  whole tier, not just the new test, is for.

## 4. Reuse the shared fixture corpus — don't invent a new sample file per test

`.ai-context/tests/fixtures/sample-repo/` already covers every
previously-identified tricky chunking/indexing shape (see its own
`README.md`). Extend it — add a new fixture file — only when a genuinely
new shape needs coverage nothing existing already provides. Reusing one
corpus across all three tiers is what lets a Tier 3 failure be
root-caused via the matching Tier 1/2 assertion on the exact same fixture.

## 5. Don't hide a discovered bug — document it as a tripwire test instead

If writing or running a test reveals a pre-existing bug unrelated to your
current task:
- Do **not** silently adjust your test's expectation to match the buggy
  behavior without comment.
- Do **not** silently fix the production bug either, unless it's tightly
  coupled to the change you're already making (that's scope creep
  otherwise — flag it instead).
- **Do** assert the current (buggy) behavior explicitly, with a comment
  explaining it's a known bug/gap — see `docs/testing.md`'s "Known gaps"
  section for existing examples of this pattern (Kotlin `interface`
  misclassification, the `TEST_FILE_PATTERN` gap, backup/restore's
  `symbol_index` coverage gap, etc.). This keeps the suite honest about
  what the code actually does, stops the bug from regressing further
  unnoticed, and tells whoever eventually fixes it exactly which test to
  update.

## 6. Clean up every time — verify, don't assume

Tier 2/3 tests spin up real Docker containers. `scripts/test.sh` already
wraps `integration`/`e2e` runs in pre-flight + trap-based cleanup, so you
don't need to call `scripts/test-cleanup.sh` yourself in the normal case.
If a run was interrupted (killed session, an uncaught Ctrl-C), run
`scripts/test-cleanup.sh` before starting a new one. Use
`scripts/test-cleanup.sh --check` to confirm the machine is actually clean
rather than assume the trap fired.
