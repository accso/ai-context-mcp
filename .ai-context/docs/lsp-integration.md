# LSP Integration — Reference

How `resolve_symbol` and its supporting LSP Bridge service work: architecture,
per-language support, licensing, and dependency resolution for "jump into
external dependency code." This is reference documentation for a fully
shipped feature — see [Architecture Deep-Dive](architecture-deep-dive.md) for
how `resolve_symbol` fits into the overall MCP tool surface, and
[`README.md`](../README.md) for the top-level pointer.

## Architecture

```
Agent
  │  MCP over StreamableHTTP
  ▼
MCP Server (one consolidated process per repo, serving all of that
            repo's configured projects)
  │  resolve_symbol(file_path, line, character, kind)
  │        kind: "definition" | "references" | "hover" | "diagnostics"
  ▼
LSP Bridge — ONE shared service PER REPO (not per host, and not per
             project within a repo — see "Why per-repo" below)
  │  - HTTP API keyed by {project_key, language}
  │  - owns a POOL of language-server CHILD PROCESSES, one live process
  │    per (project_key, language) pair — never shared across projects
  │  - lazily spawns a child process on first resolve_symbol call for a
  │    given (project_key, language); idle-evicts it after N minutes
  │    (config: `lsp.idle_timeout_minutes`, default 15)
  │  - speaks LSP JSON-RPC over stdio to each child process
  │  - normalizes responses to the same file_path/line convention already
  │    used by get_file/repo_structure (relative_path, 1-based lines)
  ▼
Language server child processes (one per project+language, subprocesses)
```

### Why per-repo, not host-wide like the inference service

The bridge/orchestration layer (management API, process-pool logic,
idle-eviction, Docker image, pinned binaries) is shared, **one instance per
repo** — deliberately *not* an inference-service-style host-wide singleton. The
inference service is a pure network service with zero filesystem access, so one
host-wide instance costs nothing extra. The LSP bridge, in contrast, must
bind-mount each project's real files on disk to root a language server at
`rootUri` — and Docker containers cannot have a bind mount added to an
already-running container without being recreated. Per-repo scoping avoids
a disruptive recreate of one shared container (and therefore a restart of
every *other* repo's in-flight language-server state) every time a new repo
starts being worked on: the bridge joins the existing
`${REPO_SLUG}_context_mcp` / `${REPO_SLUG}_context_watcher` lifecycle group
(same `scripts/start.sh` run, same mount-computation logic already used for
the watcher).

### Why per-project processes, never shared across projects

1. **Each process holds workspace-specific in-memory state.** `tsserver`,
   `jedi`, `rust-analyzer`, etc. are initialized against a single `rootUri`
   and build a project-wide symbol/type model for *that* codebase — closer
   to an open editor window than to shared stateless infrastructure.
2. **Isolation/blast-radius.** A malformed or huge file in one project can
   hang, OOM, or crash a language server. Per-project processes contain the
   blast radius to the one project.
3. **Data isolation.** This project's stated design goal is "private code
   must stay private" — a single process holding the parsed/typed contents
   of multiple projects simultaneously would be a deliberate design
   tradeoff, not something to fall into accidentally for memory savings.

Net effect: process **count** scales with the number of *concurrently
active* (project, language) pairs, not with the total number of indexed
projects — idle eviction is what actually bounds memory, not process
sharing.

### Key design choices

- **Additive, not replacing.** `symbol_index`/tree-sitter (see "Symbol
  Index" in [Architecture Deep-Dive](architecture-deep-dive.md)) stays the
  always-on, zero-cold-start default. `resolve_symbol` is a slower, opt-in,
  higher-fidelity tool the agent reaches for only when it needs cross-file
  certainty.
- **Lazy start, per (project, language) pair** — no language-server child
  process starts until the first `resolve_symbol` call touching that
  project/language.
- **`lsp.enabled_languages` is always auto-detected**, never a hand-set
  `config.yaml` field — derived from the project's own files at validate
  time (`scripts/validate_config.py:_detect_lsp_languages`), same principle
  as `profile` (see "Profile auto-detection" in
  [Project Configuration](configuration.md)).
- **`StillStarting` / retry-after pattern** — a slow first cold start (or a
  slow first-time dependency restore, see below) reports "still starting,
  retry in Ns" to the agent instead of a generic timeout, rather than
  building new signaling for the dependency-restore case specifically.

## Per-language support matrix

| Language | Server | License | Notes |
|---|---|---|---|
| TypeScript/JS | `typescript-language-server` (wraps `tsserver`) | MIT | |
| Python | `jedi-language-server` | MIT | multilspy 0.0.15 dispatches Python here, not `pyright` |
| Rust | `rust-analyzer` | MIT / Apache-2.0 (dual) | Needed a thin arm64 override for a missing manifest row |
| C# | OmniSharp (`omnisharp-roslyn`) | MIT | Same arm64 override pattern as Rust |
| Kotlin | `kotlin-language-server` (fwcd, community) | MIT | Confirmed via GitHub's license API, not just the repo badge |
| Ruby | Solargraph | MIT | No override needed — multilspy's own class has no platform-specific logic |
| C/C++ | `clangd` (LLVM) | Apache-2.0 WITH LLVM-exception | First from-scratch subclass — no multilspy dispatch exists for C/C++ at all; installed via `apt`, native arm64 build, no arm64-specific handling needed |
| Swift | `sourcekit-lsp` (Apple) | Apache-2.0 | Second from-scratch subclass; installed via Apple's official toolchain tarball (~3.3GB); needs a writable `--scratch-path` since project roots are mounted read-only |
| Go | `gopls` (`golang.org/x/tools`) | **BSD-3-Clause** | Licensing exception, shipped by explicit product-owner decision (see "Licensing" below) |
| Dart | `dart-language-server` (bundled with Dart SDK) | **BSD-3-Clause** | Same licensing-exception basis as Go; needed a thin arm64 override for a missing manifest row |
| Java | Eclipse JDT Language Server (via redhat-developer's `vscode-java` bundle) | **EPL-2.0** | Licensing exception — **not auto-enabled**; requires the deploying user's own `lsp.accept_java_epl_license: true` opt-in, never baked into any image we publish; subprocess-only isolation; see "Licensing — Java/EPL-2.0" below for the full analysis and the still-open recommendation |

All language-server binaries are pinned at Docker build time. `diagnostics`
(the 4th `kind`) is not implemented — not in multilspy's covered request
set for all languages; parked, not blocking.

## Licensing

The project's baseline constraint for this feature is MIT/Apache-2.0-only
dependencies. Three exceptions were made by explicit product-owner/user
decision, each treated differently:

- **Go (`gopls`) and Dart (`dart-language-server`) — BSD-3-Clause.**
  Treated as low-risk: BSD-3-Clause is functionally almost identical to
  MIT (permissive, no copyleft, only adds a non-endorsement clause). One
  real substantive gap: neither MIT nor BSD-3-Clause include an express
  patent grant, while Apache-2.0 does — but since MIT itself doesn't
  either, BSD-3-Clause's practical risk profile is no worse than MIT's.

### Licensing — Java / EPL-2.0

**This is the canonical record of this analysis — nowhere else in the
repo restates it; `lsp-bridge/main.py` points here rather than duplicating
it.**

Eclipse JDT Language Server (`eclipse.jdt.ls`) is EPL-2.0 — the de facto
standard Java language server (backs VS Code's own official Java support
via Red Hat's `vscode-java`, also EPL-2.0), using subprocess-only,
unmodified-binary isolation.

**Not auto-enabled, not bundled by us.** Unlike every other language here,
Java is excluded from `_detect_lsp_languages()`'s file-count auto-detection
by default (see `LSP_LANGUAGES_REQUIRING_EXPLICIT_OPT_IN` in
`scripts/validate_config.py`) — a project with `.java` files does **not**
get Java code intelligence unless the deploying user explicitly opts in,
either by setting `lsp.accept_java_epl_license: true` in their own
`config.yaml` (see [Configuration](configuration.md)), or interactively:
`scripts/ai-context-up.sh` detects `java_epl_pending` in `.resolved.json`
and prompts per project (`Enable Java code intelligence for '<project>'?
[y/N]`, default **N**) before doing anything else. Accepting writes a
persisted `.java-epl-accepted-<project>` marker file (mirrors the existing
`.restore-choice-<project>` convention rather than rewriting the
heavily-commented config.yaml) and re-validates config immediately, so the
choice is remembered on future runs without asking again. The prompt is
skipped (Java stays disabled) in non-interactive environments — CI, or any
run without a TTY. Only an explicit "yes" — via either path — triggers the
Docker build step that downloads the unmodified upstream
`vscode-java`/`eclipse.jdt.ls` binary directly from Red Hat's servers, onto
the user's own machine. We never publish a pre-built image with Java baked
in. This keeps EPL-2.0 out of anything *we* distribute — see
`THIRD-PARTY-NOTICES.md` — while still letting a user who wants Java
support turn it on. What EPL-2.0 actually restricts, re-checked against
the Eclipse Foundation's own
EPL-2.0 FAQ:

1. **Scope is file-level, not project-level.** Only EPL-covered files you
   *modify* must stay EPL and have their source made available to
   recipients. Code that merely *uses* the server — launching an
   unmodified `eclipse.jdt.ls` jar as a separate subprocess over stdio —
   is not "Derivative Works" under EPL's own definition. This is the
   strongest argument for the subprocess-only approach here, but it is a
   legal reading, not a settled fact for this specific deployment.
2. **"Secondary Licenses" trap (new in EPL-2.0 vs 1.0):** a project can
   *opt in* to letting recipients relicense under GPL-family terms.
   `eclipse.jdt.ls` does not do this today, but it is a per-project opt-in
   that must be re-checked on every version bump, not assumed permanent.
3. **Patent peace clause, not pure grant:** contributors grant a patent
   license, but it **terminates automatically if the recipient sues anyone
   over patents related to the software.** Low practical risk for a
   dev-tool consumer, but a materially different shape than Apache-2.0's
   patent grant.
4. **The fact pattern that actually decides this: distribution.** EPL's
   copyleft/source-availability obligations are triggered by *distributing*
   the covered work. Because Java requires the deploying user's own
   explicit `lsp.accept_java_epl_license: true` opt-in (see above) and we
   never publish a pre-built image with it baked in, **we are not the ones
   distributing `eclipse.jdt.ls`** — each user's own `docker build` fetches
   it directly from Red Hat's servers onto their own machine. The remaining
   open question is scoped to *that user's* environment: if a user who
   opted in then redistributes their own built image (e.g. ships it to a
   client), that is plausibly a distribution event on their part and
   changes the analysis for them — this is not a fixed, permanent
   deployment model we can vouch for on their behalf.

**Still open: a real legal/compliance review of this subprocess-only,
opt-in EPL-2.0 reading has not happened** — explicitly scoped to whether a
user's own build/run of this feature, or any further redistribution by
that user, is safe under EPL-2.0. This was an engineering/product
decision, not a substitute for that review. Do not treat "opt-in +
subprocess-only makes EPL-2.0 automatically fine" as settled without that
sign-off.

An MIT-licensed alternative (`georgewfraser/java-language-server`, built on
the Java Compiler API rather than JDT) was evaluated and **not adopted**:
genuinely active (verified via GitHub API) but with a bus-factor of
effectively 1 (one author holds the overwhelming majority of commits) — a
real continuity risk for a long-term dependency, and no native multilspy
dispatch (would need a from-scratch subclass, same cost as the JDT
integration already shipped).

## Dependency resolution — "jump into external dependency code"

`resolve_symbol` dispatches correctly to the real per-language server for
every language above, but for almost every language except TypeScript/JS a
lookup that resolves *into external dependency code* comes back empty
unless the dependency has actually been restored/fetched first — the
dependency's source files simply don't exist inside the container
otherwise. TypeScript/JS "just works" because `npm install` puts
`node_modules` inside `root_path` (already inside the read-only project
mount) by accident of where npm puts things, not by design.

| Language | Package manager | Cache location | Restore command | Real source recoverable? |
|---|---|---|---|---|
| TypeScript/JS | npm | `node_modules` (inside `root_path`, accidental) | `npm ci` if missing/stale | Yes (`.d.ts` + often real `.ts`/sourcemaps) |
| Python | pip | project-namespaced site-packages | `pip install` into a per-project cache | Yes — pure-Python packages ship real `.py` |
| Go | go modules | external cache volume | `go mod download` | Yes — real `.go` source |
| Rust | cargo | external cache volume | `cargo fetch` | Yes — real `.rs` source |
| C# | NuGet | external cache volume + `obj`/`bin` project-tree write | `dotnet restore` (OmniSharp needs intermediate-output paths redirected via environment variables, not just `-p:` flags) | Partial — real source only if the package ships SourceLink/embedded PDBs; OmniSharp decompiles otherwise |
| Ruby | bundler/gem | `GEM_PATH` + `BUNDLE_APP_CONFIG`-redirected cache | `bundle install` | Yes — real `.rb` source, including validated solargraph static-analysis behavior on untyped params |
| Dart | pub | `PUB_CACHE`-redirected cache | `dart pub get` | Yes — real `.dart` source. `pubspec.lock` write genuinely can't be redirected; the restore step fails that one step gracefully (logged warning, no crash) and degrades to empty dependency-code results, same as any unresolved case |
| Swift | SPM | `.build/checkouts` (own scratch path) | `swift package resolve` — sourcekit-lsp performs its own complete, independent resolution | Yes — real `.swift` source, zero wiring changes needed |
| Java | Maven/Gradle | `~/.m2` / `~/.gradle/caches` + project-local `tmpfs` overlay for `<project>/target/`/`<project>/.gradle/` | `mvn dependency:resolve -Dclassifier=sources` / Gradle sources task | Yes — real decompiled/source-jar output, once JDTLS's separate build-tool-state write requirement is satisfied |
| Kotlin | Gradle | same as Java + Gradle sources task | Same as Java | Yes — real decompiled source. Needed a JVM process-launch workaround (`-Djdk.lang.Process.launchMechanism=FORK`) in addition to the `tmpfs` overlay |
| C/C++ | none uniform (system headers / optionally vcpkg/conan) | N/A | N/A | Out of scope for v1 — no single dependency-cache model applies the way it does for the other languages here |

**Persistent per-ecosystem cache volumes** (shared across all registered
projects, same rationale as the shared Qdrant/Postgres volumes — dependency
versions overlap heavily across projects, matching a real developer
machine): `ai_context_go_mod_cache`, `ai_context_cargo_registry`,
`ai_context_nuget_packages`, `ai_context_gem_cache`, `ai_context_pub_cache`,
`ai_context_m2_gradle_cache`. Mounted at each tool's default cache path in
the LSP container (`GOPATH`, `CARGO_HOME`, `NUGET_PACKAGES`, `BUNDLE_PATH`,
`PUB_CACHE`, `~/.m2` + `~/.gradle`) via `scripts/start.sh`'s `LSP_ARGS`
construction.

**Staleness gating:** on each spawn, the bridge hashes the project's
lockfile (`package-lock.json`/`requirements.txt`/`go.sum`/`Cargo.lock`/…)
and compares against the last-restored hash. If unchanged, the restore step
is skipped entirely (fast path, matches normal spawn latency). If changed
or absent, the restore command runs synchronously before starting the
language server, then the new hash is persisted. A slow first-time restore
(e.g. `go mod download` on a large module graph) uses the same
`StillStarting`/retry-after mechanism as a slow LSP cold start (see
Architecture above) rather than a generic timeout.

**Java/Kotlin project-import fix:** JDTLS's Maven project-import and
kotlin-language-server's Gradle classpath resolution each unconditionally
try to create project-local build-tool state (`<project>/target/`,
`<project>/.gradle/`) directly inside the read-only project mount, and fail
outright (a complete project-import failure, not just degraded dependency
lookup) if that write fails. Fixed via project-scoped `tmpfs` overlays in
`scripts/start.sh`, gated on each project's own `lsp.enabled_languages`.
