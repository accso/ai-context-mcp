# Testing `.ai-context`

Three independently-runnable tiers, so an AI coding agent (or a human) can
match verification cost to the blast radius of a change — matching the
"tight verify-and-correct loop" discipline in
`.github/instructions/ai-workflow.instructions.md`. Scope note:
`.ai-monitoring` and `.ai-benchmark` are separate tools moving to their own
repos and are **not** covered here.

**This document is the reference** (tiers, commands, fixture corpus,
environment setup). For the test-driven **workflow** to follow when making
a change — red/green/refactor, which tier to add a test to, how to handle
a discovered bug — see
`.github/instructions/ai-context-tdd.instructions.md`.

> **`opencode-ai-context-plugin/` has its own, simpler test setup.** Since
> the plugin has no Postgres/Qdrant/Docker dependency at all (SQLite,
> in-process), its three-tier *workflow discipline* still applies (see
> `.github/instructions/ai-context-tdd.instructions.md`, which covers both
> projects) but its actual runner is single-tier:
> `opencode-ai-context-plugin/test/unit/*.test.ts`, run via
> `npm run test:unit` (plain `node --test`, no `scripts/test.sh`, no
> Docker, no fixture corpus separate from this one — everything the
> plugin does is fast enough to be "Tier 1" in practice). Don't run this
> doc's `scripts/test.sh` for plugin changes, and don't run
> `npm run test:unit` for changes to this Docker stack — the two test
> suites are entirely independent.

## Quick start

```bash
# One-time setup (see "Environment setup" below for details)
pip install -r requirements-test.txt -r watcher/requirements.txt \
  -r lsp-bridge/requirements.txt -r scripts/requirements.txt
cd mcp-server && npm install && cd ..

# Run a tier
scripts/test.sh unit                 # Tier 1, every component — seconds, no Docker
scripts/test.sh unit watcher         # Tier 1, just watcher/
scripts/test.sh integration          # Tier 1+2 — adds disposable Postgres/Qdrant
scripts/test.sh e2e                  # Tier 1+2+3 — adds the full pipeline, minutes
scripts/test.sh all                  # every tier
```

## Which tier to run for which change

| You changed... | Run |
|---|---|
| `chunker.py`, `symbol_extractor.py`, `import_extractor.py`, `validate_config.py`, `context_assembler.ts`, `range_slice.ts` (pure logic, no DB/network) | `scripts/test.sh unit` |
| `db.ts`'s `analyzeImpact`, `indexer.py`'s delta-upsert/self-heal, `dependency_restore.py`, `inference/main.py` (backend selection/routing) | `scripts/test.sh integration` |
| Anything crossing components (indexing → MCP tool response), `backup.py`, `lsp-bridge/main.py`'s routing | `scripts/test.sh e2e` |
| Not sure / touched multiple areas | `scripts/test.sh all` |

`analyze_impact` (the real MCP tool, not the test) is a good way to double-check
blast radius before picking a tier — if it shows callers/callees outside the
file you changed, favor the broader tier.

## The three tiers

| Tier | What | Needs | Speed |
|---|---|---|---|
| **1 — Unit** | Pure functions: chunk boundaries, symbol/import extraction, config validation, query expansion, keyword rerank, line-range slicing, ColBERT MaxSim scoring (`inference/maxsim.py`) | Nothing — no Docker, no network | Seconds |
| **2 — Integration** | Logic touching Postgres/Qdrant: `analyze_impact` traversal, indexer delta-upsert/self-heal, dependency-restore command selection, inference service backend selection/routing | Disposable Postgres/Qdrant containers (or mocked subprocess/HTTP) | Tens of seconds |
| **3 — E2E** | Full pipeline: real indexing → real MCP tools over real HTTP+MCP protocol; backup/restore round-trip | Real Docker containers (Postgres, Qdrant) + Python/Node processes | Minutes |

### Layout

```
.ai-context/
  pytest.ini                          Python test config (all components)
  requirements-test.txt               pytest, pytest-asyncio, responses
  watcher/tests/{unit,integration}/
  lsp-bridge/tests/{unit,integration}/
  scripts/tests/unit/
  inference/tests/{unit,integration}/    unit: maxsim.py scoring (pure logic)
                                          integration: main.py routing/backends
  mcp-server/test/{unit,integration,e2e}/
  tests/fixtures/sample-repo/          shared multi-language fixture corpus
  tests/e2e/*.py                       Python helper scripts the TS E2E
                                        tests spawn as subprocesses (real
                                        watcher indexing/backup code, run
                                        out-of-process since this codebase's
                                        watcher is Python and the E2E test
                                        harness lives in TS)
  scripts/test.sh                      tier-selectable runner (this doc's
                                        "Quick start" commands)
  scripts/test-cleanup.sh              idempotent Docker cleanup — see below
```

## The shared fixture corpus

`tests/fixtures/sample-repo/` (see its own `README.md`) is one small,
deliberately-crafted multi-language repo referenced by **all three tiers** —
a Kotlin file (regex-fallback chunking), a TS file with a decorator +
switch statement (tree-sitter + dispatch-arm sub-chunking), a Vue SFC, a
Python file with `match`, a Java/C# pair, and two Markdown docs (above/below
the tree-index heading threshold). Tier 1 asserts chunk boundaries directly
on these files; Tier 2 asserts DB rows after indexing them; Tier 3 asserts
MCP tool responses after the real pipeline indexes them. This means a Tier
3 failure can usually be root-caused by checking whether the matching Tier
1/2 assertion on the same fixture still passes.

Keep this corpus small and deliberate — add a file only when a new
chunking/indexing behavior needs fixture coverage nothing existing already
provides.

## Environment setup

- **Python**: `pip install -r requirements-test.txt -r watcher/requirements.txt -r lsp-bridge/requirements.txt -r scripts/requirements.txt -r inference/requirements.txt` (the last one is fully optional now — `inference`'s Tier 1/2 suites monkeypatch model loading entirely, so neither `fastembed` nor `onnxruntime` need to be installed to run them; only needed if you want to actually exercise real model inference locally outside Docker). Requires **Python 3.12** (`mise install python@3.12` — pinned in `mise.toml` to match the Docker images; `tree-sitter-languages` doesn't yet support 3.13+).
- **TypeScript**: `cd mcp-server && npm install`. No extra test framework — `node --test` (built-in) + the already-present `ts-node` devDependency.
- **Docker**: required for Tier 2 (`analyze_impact`, `indexer.py`, `inference` — Postgres/Qdrant) and Tier 3 (adds the full pipeline). Not needed for Tier 1 at all.

## Cleanup — no leftover containers

Every Tier 2/3 Docker resource carries `--label com.ai-context.test=true`
(or runs under a container name matching `ai_context_test_*`), on ports
distinct from the real dev stack (`5433`/`6333`) — confirmed safe to run
**concurrently** with an already-running real dev stack.

- `scripts/test-cleanup.sh` — idempotent, safe to run any time. Run it if
  a previous test run was interrupted/crashed (an agent's own shell
  session being killed mid-run is a real scenario, not hypothetical).
- `scripts/test-cleanup.sh --check` — lists any lingering test resources
  without removing them (exits non-zero if anything is found) — use this
  to *verify* the machine is clean instead of assuming.
- `scripts/test.sh` already runs cleanup as a pre-flight step before every
  `integration`/`e2e` invocation, and traps `EXIT`/`INT`/`TERM` to run it
  again afterward — this is automatic, you don't need to call
  `test-cleanup.sh` yourself in the normal case.

## Known gaps / accepted limitations (found while writing these tests)

These are documented as explicit "known gap" tests (not silently patched
over) so they can't regress further unnoticed. Fixing them is a separate,
future change — out of scope for the test suite itself:

- **Kotlin `interface` misclassified as `class`** in `symbol_extractor.py`
  (tree-sitter Kotlin grammar emits `class_declaration` for both) — see
  `watcher/tests/unit/test_symbol_extractor.py`.
- **`TEST_FILE_PATTERN`** (`db.ts`, used by `analyze_impact`'s test-coverage
  detection) only recognizes JS/TS conventions, not Python's `test_*.py` or
  a top-level `tests/` directory — see
  `mcp-server/test/integration/analyze_impact.test.ts`.
- **Backup/restore doesn't cover `symbol_index`/`import_edges`/`call_edges`**
  — only a reindex rebuilds those after a restore — see
  `mcp-server/test/e2e/backup_restore.test.ts`.
- **Real multilspy+jedi-language-server round-trip hangs** in this
  sandboxed test environment at the LSP `initialize` handshake — the
  `resolve_symbol` test suite (`lsp-bridge/tests/integration/test_main_resolve_symbol.py`)
  covers this codebase's own routing/validation logic with the language
  server mocked instead. Real round-trip testing needs a different
  environment (e.g. the actual Docker image) or further debugging.
- **`context_assembler.ts`'s `deriveQdrantUrl()`** reads `process.env.QDRANT_URL`
  directly instead of the `qdrantUrl` value threaded through
  `SharedServices`/`createProjectServer` (unlike `get_file`, which uses the
  injected value correctly) — harmless in production (both are always set
  from the same source at container startup) but a real DI inconsistency,
  found via `mcp-server/test/e2e/test-stack.ts`.
