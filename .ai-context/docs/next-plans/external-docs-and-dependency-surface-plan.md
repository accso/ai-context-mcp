# External Documentation & Dependency-Surface Indexing

Status: proposed
Depends on: nothing (additive); reuses the existing chunker / doc_tree_builder /
Qdrant / Postgres pipeline unchanged
Licensing constraint: every new dependency must be MIT or Apache-2.0

## 0. Problem statement

Today the indexer only ever sees files that already live inside the mounted
project root. An agent working against, say, C# 12 / EF Core 9, or a Kotlin
library the team wrote three years ago, has no grounded source of truth for
either (a) the language/library's own official docs, or (b) the exact public
surface of the versions actually pinned in the project. It falls back to
training-data recall, which is stale, unversioned, and sometimes flat-out
wrong for fast-moving ecosystems.

This plan closes that gap with three independent, opt-in pillars:

1. **External docs crawling** — official docs (and optionally curated
   secondary sources) for languages/libraries, versioned, crawled once and
   re-crawled on version drift.
2. **Out-of-root local docs** — docs that already exist as files but don't
   live under the project root (e.g. a company-wide standards repo).
3. **Dependency-surface indexing** — the public API surface (not full
   source) of the project's own *direct* dependencies, so the agent can see
   real signatures instead of guessing them.

Pillar 2 is nearly free — it's the existing `extra_gitignore_paths` bind-mount
pattern applied to a new purpose. Pillars 1 and 3 are the substantive work.

### 0.1 Relationship to the LSP plan — and recommended build order

`docs/lsp-integration.md` already covers `hover` (exact type/signature at a
location) and go-to-definition *into* dependency code (site-packages, Cargo
cache, NuGet cache, …). That's the same underlying question Pillar 3
(dependency-surface indexing) answers — just live/on-demand instead of
pre-indexed. The two are complementary, not redundant:

- **LSP** gives an exact answer for a symbol the agent already has a
  location for, but requires a running language server.
- **Pillar 3** gives a pre-indexed, plain-text-searchable surface — useful
  for *discovering* a symbol via `semantic_search` before you know its exact
  name/location, and works even where no LSP integration exists yet for
  that ecosystem.

Recommended cross-plan build order:

1. **LSP integration** (now shipped, see `docs/lsp-integration.md`) first.
   It's fully standalone, and it became the extraction engine for Pillar 3 (real
   `hover`/`definition` output is more accurate than parsing raw
   `.d.ts`/`.pyi`/XML-doc sidecars with tree-sitter) — building Pillar 3
   before LSP means redoing that extraction path later.
2. **Call/import graph + Tier-1 chunking** (now shipped, see
   `docs/architecture-deep-dive.md`) next (or in parallel) — pure
   tree-sitter, no LSP dependency, no reason to wait.
3. **This plan's Pillars 1 and 2** (external docs crawling, out-of-root
   local docs) — fully independent of LSP, can ship any time, including
   before LSP.
4. **This plan's Pillar 3** (dependency-surface indexing) — deliberately
   sequenced *after* LSP lands, for the extraction-engine reuse above
   (real `hover`/`definition` output via the shipped `resolve_symbol` tool
   is more accurate than parsing raw `.d.ts`/`.pyi`/XML-doc sidecars).
   **Correction:** the language/manifest-detection logic Pillar 3 needs
   (§3.2, `manifest_reader.py`) is *not* shared with the LSP bridge's
   existing detection — the LSP bridge (`_detect_lsp_languages` in
   `validate_config.py`) detects languages purely from a file-extension
   census, never reads manifests/lockfiles. `manifest_reader.py` (parsing
   `package-lock.json`/`*.csproj`/`poetry.lock`/etc. for direct-dependency
   *versions*, not just language presence) is genuinely new scope, shared
   only between this plan's own Pillar 1 (§1.2) and Pillar 3 (§3.1) — not
   with the LSP bridge.

**Design principle carried through all three pillars:** materialize content
as ordinary Markdown/source files on disk, in a synthetic subtree, and then
run the *existing* indexing pipeline (chunker → symbol/doc-tree builder →
embedder → Qdrant/Postgres) over them unmodified. This is the same principle
already used in `remote-repo-server-plan.md` (materialize a working copy,
then index it like any other checkout). It means pillars 1 and 3 are mostly
"how do I get the right files onto disk with the right metadata", not "how do
I build a second indexing pipeline".

## 1. Pillar 1 — External docs crawling

### 1.1 Config shape

```yaml
projects:
  - name: my-service
    external_docs:
      enabled: false          # opt-in per project
      # Version is auto-resolved from the project's own manifest/lockfile
      # wherever possible (see 1.2). Only give an explicit version when it
      # can't be resolved that way (e.g. the language itself, not a package).
      sources:
        - library: csharp            # matches a manifest-resolvable name where possible
          version: "12"               # explicit fallback — language version, not a NuGet package
          source: "https://learn.microsoft.com/en-us/dotnet/csharp/"
        - library: "effective-kotlin" # a blog/book series, not on any package manager
          source: "https://blog.kotlin-academy.com/tagged/effective-kotlin"
          trust: secondary             # see 1.5
        - library: fastapi             # resolvable from requirements.txt/poetry.lock
          source: "https://fastapi.tiangolo.com/"
      recrawl_on_version_change: true   # see 1.6
```

`library`/`source` are required; `version` is optional and only needed when it
can't be resolved automatically (see 1.2). This directly reuses the
`extra_skip_paths`/`extra_gitignore_paths` convention of "empty list unless a
project opts in".

### 1.2 Version resolution

Two different questions, deliberately kept separate:

- **"Which version am I actually using?"** — must be automatic. Read from
  the manifest/lockfile already present in the project root
  (`package-lock.json`/`pnpm-lock.yaml`, `*.csproj`/`packages.lock.json`,
  `poetry.lock`/`requirements.txt`, `go.sum`, `Cargo.lock`, `pom.xml`/
  `build.gradle.kts`). This reuses the same manifest-parsing pillar 3 needs
  (§3.2) — one shared `manifest_reader.py` module serves both pillars.
- **"Where do I crawl for that version's docs?"** — stays config-driven.
  Guessing doc URLs from a package name is unreliable (see research notes
  below); a human-supplied `source` is the pragmatic, low-maintenance answer.

If a `library` entry matches a manifest-resolvable dependency, the resolved
version from the lockfile wins over any `version:` in config (config
`version:` is only a fallback for entries that aren't manifest-resolvable,
like a language version or a blog series).

### 1.3 Crawl strategy

Prior art checked (all MIT/Apache-2.0 relevant, or informational only):

- **Context7** (github.com/upstash/context7) — the public GitHub repo is
  *only* the MCP client. The crawl/index engine is closed-source SaaS. Not
  reusable, but validates the product shape.
- **docs-mcp-server** (github.com/arabold/docs-mcp-server, MIT) — explicitly
  positions itself as "the open-source alternative to Context7, Nia and
  Ref.Tools". Self-hosted, does version-aware crawling of websites, GitHub,
  npm, PyPI and local files, and auto-probes `/llms.txt` at a doc root as a
  curated crawl seed. This is the closest existing implementation to what we
  need; worth vendoring ideas from (crawl-frontier + readability extraction),
  not necessarily the whole project, since our storage/indexing pipeline is
  already bespoke (Qdrant + PageIndex-style doc tree).
- **llms.txt** (llmstxt.org) — a proposed convention: a curated
  `https://<site>/llms.txt` Markdown index of a docs site's own pages, often
  with `.md`-suffixed twin pages for each HTML page. When present, this is a
  reliable, low-noise crawl seed *within* an already-known site. It does
  **not** solve "which site to crawl for library X" — that's still `source:`
  in config.

Crawl algorithm per `source` entry:

1. Try `GET <source>/llms.txt` (and common variants: site root, one path
   segment up). If found, treat every linked page as the crawl frontier
   (bounded, no further discovery needed).
2. Otherwise, do a bounded same-origin crawl starting at `source`: follow
   in-content links under the same path prefix, depth-limited (default 3),
   page-count-limited (default 300, configurable), respecting `robots.txt`.
3. For each fetched page: prefer the `.md` twin if `llms.txt` indicated one
   exists (already Markdown, no extraction loss); otherwise run HTML→Markdown
   normalization (strip nav/footer/ads via a readability-style extraction —
   e.g. `unstructured` (Apache-2.0) or a small in-house readability pass —
   keep code blocks verbatim).
4. Write each page to
   `<external-docs-cache>/<library>/<version>/<slug>.md`, with YAML
   frontmatter (`source_url`, `library`, `version`, `crawled_at`).

### 1.3.1 Crawl safety: network scope (SSRF) and content-licensing

Confirmed as required, distinct concerns:

- **SSRF protection.** The crawler runs server-side with outbound network
  access, driven by a `source:` URL that lives in project config. A
  careless or malicious config entry could point it at internal
  infrastructure (`http://169.254.169.254/`, `http://localhost:...`,
  private RFC1918 ranges, etc.). Mandatory guardrails: allow only
  `https://` scheme, resolve the hostname and reject any resolution into a
  private/link-local/loopback IP range before connecting, and reject
  redirects that leave the allowed scope. This applies per-request, not
  just to the initial `source:` URL, since a same-origin crawl still
  resolves DNS on every hop.
- **Content copyright — distinct from the MIT/Apache-2.0 software-license
  constraint.** That constraint governs *code dependencies* we add to the
  project; it does not license our use of *crawled prose*. Storing full-text
  mirrors of copyrighted documentation (Microsoft Learn pages, blog posts)
  is a copyright question, not a software-license one. Mitigations for v1:
  honor `robots.txt` `Disallow` unconditionally (no override), keep crawled
  text server-side only (never re-published outside the project's own
  retrieval tool), and always carry `source_url` through to the retrieved
  chunk (§1.5) so results are attributed back to the original page rather
  than presented as ours. This is a reasonable-use posture for an internal
  developer tool, not a legal sign-off — flag for a real license/compliance
  review before this ships broadly, especially for any `trust: secondary`
  (blog/community) source.

### 1.4 Where this hooks into the pipeline

Confirmed from `entrypoint.py`: `MODE` is a one-shot dispatch
(`full`/`l0`/`watch`/`restore`/`backup`), invoked via `docker run -e MODE=...`
from `scripts/init.sh` / `scripts/restore.sh` — not a long-running
compose service (except `watch`). A new `MODE=external_docs` fits this
pattern exactly:

- New one-shot mode, added to the same dispatch table in `entrypoint.py`.
- Triggered by: (a) `scripts/init.sh` once at project onboarding if
  `external_docs.enabled`, and (b) a lightweight periodic re-check (cron /
  CI schedule / manual `scripts/refresh_external_docs.sh`) — not on every
  file-watch debounce cycle, since crawling is network-bound and version
  changes are rare compared to code edits.
- The crawl step (§1.3) writes Markdown into a synthetic cache directory
  mounted alongside the project root (same bind-mount mechanism as
  `extra_gitignore_paths`, i.e. a fixed in-container path like
  `/external-docs/<library>/<version>/`), tagged in the file's frontmatter.
- The **existing** `full`/`l0` indexing code then treats these paths like
  any other Markdown under `always_tree_paths` (`doc_index` already tree-
  indexes `docs/`, `wiki/`, `adr/`, etc. — the external-docs cache directory
  is simply added to that list). No new chunker, embedder, or storage code.

### 1.5 Retrieval surface

Reuse `semantic_search`, don't add a new MCP tool — the same principle
already used for cross-project `sources:` federation (blended results from
other locally-indexed projects, capped depth). Concretely:

- Every chunk/tree-node originating from an external-docs page carries
  `source_type: external_doc`, `library`, `version`, `trust` (`official`
  default, `secondary` for opted-in blog/community sources) in its Postgres
  row and Qdrant payload.
- `semantic_search` results are ranked normally; a `trust: secondary` chunk
  is never preferred over an `official` one at equal relevance (tie-break
  only, not a query-time filter) — keeps the door open for blog content
  without letting stale/unreliable material silently outrank vendor docs.
- No query-time library filter is required in v1; if false-positive noise
  turns out to be a problem in practice, add an optional `doc_scope` filter
  parameter later (defer — avoid speculative API surface, matching this
  session's existing "don't build for problems you haven't measured"
  stance).

### 1.6 Re-crawl / staleness

- `recrawl_on_version_change: true` (default) — the version-resolution step
  in §1.2 diffs the resolved version against what's stored for that
  `library`, tracked in a small new table:

  ```sql
  CREATE TABLE external_doc_sources (
    project      TEXT NOT NULL,
    library      TEXT NOT NULL,
    resolved_version TEXT,
    source_url   TEXT NOT NULL,
    trust        TEXT NOT NULL DEFAULT 'official',  -- official | secondary
    last_crawled_at TIMESTAMPTZ,
    page_count   INT,
    PRIMARY KEY (project, library)
  );
  ```

  On drift, only that library's `MODE=external_docs` step re-runs, others
  are untouched.
- No open-ended re-crawl schedule for unchanged versions in v1 — docs for a
  pinned version don't change; re-crawling them on a timer buys nothing and
  costs network/compute for no benefit.

### 1.7 Storage growth visibility

No hard quota in v1 (avoid guessing at a limit before real usage data
exists). Instead: after each crawl, log `page_count` and total on-disk size
per library at **warning** level to the watcher/indexer container's Docker
logs if it exceeds a low, deliberately-noisy threshold (e.g. >50 pages or
>5 MB per library) — so growth is visible in normal container-log review
without needing a dashboard, and a real quota can be sized later from
observed data rather than guessed upfront.

## 2. Pillar 2 — Out-of-root local docs

No new mechanism needed. This is the existing `extra_gitignore_paths` bind-
mount pattern, re-labeled for docs:

```yaml
projects:
  - name: my-service
    indexer:
      extra_doc_paths: ["../company-standards/", "/shared/architecture-decisions/"]
```

These are mounted read-only at fixed in-container paths (same mechanism as
`extra_gitignore_container_paths` in `config_loader.py`) and added to
`doc_index.always_tree_paths` so they get the same PageIndex-style tree
treatment as `docs/`/`adr/`. Docs that already live inside the project root
need **zero** configuration — they're already indexed today.

## 3. Pillar 3 — Dependency-surface indexing

### 3.1 Scope (deliberately narrow, opt-in)

To avoid the two failure modes flagged during design ("too much noise" vs.
"nothing useful"):

- **Direct dependencies only** — no transitive closure. Reuses the exact
  same manifest read as §1.2 (shared `manifest_reader.py`).
- **Public API surface only, never full dependency source** — type/function
  signatures and doc-comments, not implementation bodies. This is also what
  keeps node_modules-style noise out: we extract, we don't mirror.
- **Opt-in per project**, off by default:
  ```yaml
  projects:
    - name: my-service
      index_dependency_surface: true
  ```
- **Polyglot repos**: `manifest_reader.py` scans for *all* recognized
  manifest types present in the project root, not one language per project
  — a real repo commonly mixes e.g. `package.json` (frontend) with
  `*.csproj` (backend). **Not shared with the LSP bridge** (see §0.1
  correction) — `manifest_reader.py` is net-new, purpose-built for
  version/dependency resolution (Pillars 1 and 3 only), distinct from the
  LSP bridge's file-extension-census language detection.

### 3.2 Per-ecosystem feasibility (gates what ships in v1)

> **Feasibility correction (confirmed against the running containers):**
> `scripts/start.sh` only bind-mounts each project's `root_path` (plus the
> resolved config) into the watcher/LSP containers — never a host-global
> package cache. `node_modules` normally lives *under* `root_path` (npm
> installs there), so it's visible for free. But Go's module cache
> (`$GOPATH/pkg/mod`), Rust's crate registry (`~/.cargo/registry`), and
> NuGet's global packages cache (`~/.nuget/packages`) are **global, outside
> the repo, and not mounted** — confirmed by grepping `lsp-bridge/*.py` for
> any restore step (`cargo fetch`/`go mod download`/`dotnet restore`): none
> exists today. So "real source in module/registry cache (already public)"
> is **only true for a vendored repo** (`go mod vendor`, `cargo vendor`); for
> the common non-vendored case there is currently no on-disk surface to
> extract at all — this affects the already-shipped LSP bridge's
> `resolve_symbol` hover/definition-into-dependency-code too, not just this
> plan, and should be tracked as a shared fix (§3.2a below), not duplicated.

| Ecosystem | Surface source | v1 status |
|---|---|---|
| TypeScript/JS | `.d.ts` files ship with the package, already under `root_path` (`node_modules`) | in scope, v1 — no restore step needed |
| Go | real source in module cache — **not mounted**; needs a restore step (§3.2a) unless the repo already vendors (`vendor/`, in-tree) | in scope, v1, but gated on §3.2a landing |
| Rust | real source in crate registry cache — **not mounted**; needs a restore step (§3.2a) unless the repo already vendors (`cargo vendor`, in-tree) | in scope, v1, but gated on §3.2a landing |
| Python | `.pyi` stubs, only sometimes present, and only visible if the project uses an in-repo venv (`root_path/.venv`) rather than a global/conda env | partial — v1 best-effort, skip if absent; restore step (§3.2a) helps if adopted |
| C# | XML-doc sidecar files, shipped inside the NuGet package — **not mounted** (global `~/.nuget/packages` cache); needs a restore step (§3.2a) | partial — v1 best-effort, gated on §3.2a landing |
| Java/Kotlin | no source available without an explicit `-sources.jar` | **excluded by default** — no free public surface to extract |

This table is the acceptance gate: an ecosystem only gets turned on for a
project if a real, already-present, license-safe surface artifact exists on
disk after normal dependency install. We never fetch or decompile anything
extra to manufacture a surface — the one exception is §3.2a's restore step,
which only downloads the exact versions already pinned in the project's own
lockfile (the same public artifact `npm install`/`go mod download` would
produce on a dev machine), not new/undeclared content.

### 3.2a Dependency fetch/restore step — superseded, see `docs/lsp-integration.md`

This subsection originally designed a Go/Rust/C# restore step scoped only
to Pillar 3's own extraction needs. That design has been superseded and
generalized: `docs/lsp-integration.md`'s "Dependency resolution" section is now the canonical home
for the restore-cache-volume design, covering **all** LSP languages (not
just three), because the identical gap (global dependency cache not
mounted, no restore command anywhere in `lsp-bridge/`) also silently breaks
the already-shipped `resolve_symbol` tool's hover/definition for those same
languages — a bug independent of whether Pillar 3 ever gets built.

Once that plan's Phase 1/2 land, Pillar 3's extraction step (§3.3 below)
reuses the same restored cache as its source — it does not need its own
separate restore logic. See the dependency-resolution table in `docs/lsp-integration.md` for the
full per-language root-cause table and §3 for the cache-volume/staleness
design.

### 3.3 Extraction pipeline

1. `manifest_reader.py` resolves direct dependencies + versions (shared with
   §1.2).
2. For each dependency, locate its on-disk package location:
   - TS: `node_modules/<pkg>` — already present, no extra download.
   - Go/Rust/C#: run §3.2a's restore step first (lazily, on first miss —
     see §3.2a decision 5) into the persistent restore-cache volume, then
     locate the package there. **Not** "already present after a normal
     install" as originally assumed — see §3.2 correction.
   - Python: prefer an in-repo venv (`root_path/.venv/.../site-packages`)
     if present; otherwise fall back to §3.2a's restore step.
3. Extract the public-surface artifact per the table above:
   - TS: copy `.d.ts` files as-is (already the public surface, no processing
     needed).
   - Go/Rust: run the existing `symbol_extractor.py` tree-sitter tables
     (already cover neither today — **note**: Go/Rust are not yet in
     `_EXT_TO_LANG`; extending that table is in scope here) restricted to
     exported/pub symbols only, discarding function bodies.
   - Python/C#: extract `.pyi`/XML-doc sidecar as-is where present; skip the
     dependency (not an error) where absent.
4. Materialize extracted surface files under a synthetic path, e.g.
   `<dep-surface-cache>/<ecosystem>/<package>/<version>/...`, tagged with
   `source_type: dependency_surface`, `package`, `version` — same tagging
   convention as Pillar 1.
5. Existing chunker/symbol pipeline indexes these like any other source file
   (`.d.ts`/`.py`/`.cs` are already indexed extensions) — again, no new
   storage code.

### 3.4 Retrieval surface

Same as §1.5: surfaced through the existing `semantic_search`/symbol lookup
tools, tagged `source_type: dependency_surface`, no new MCP tool. A query
like "what does `PlatformUserRepository.save` accept" can now be grounded in
the real installed signature instead of training-data recall.

## 4. Open decisions carried forward

These are flagged rather than blocking — resolve during/after a first
prototype, consistent with how this session has treated other open
questions (e.g. RRF/presets, tracked in `consolidated-backlog-and-serena-roadmap.md`):

1. **Re-crawl trigger scope** — v1 proposes automatic on manifest-detected
   version drift only (§1.6); confirm this is sufficient once used in
   anger, or whether a periodic freshness check is also wanted for
   `secondary`-trust sources whose content can change without a version bump
   (blog posts get edited).
2. **Version-auto-resolution scope** — v1 auto-resolves only
   package-manager-declared dependencies; language/runtime versions (C#,
   the JVM itself, etc.) always need the explicit config fallback. Revisit
   if a reliable signal for those emerges (e.g. `global.json`,
   `.tool-versions`).
3. **Official-only vs. + secondary sources** — v1 defaults to official docs;
   `trust: secondary` exists but is opt-in per source entry, never on by
   default. Revisit the tie-break-only ranking rule (§1.5) once there's
   real query data to look at.
4. **Extending `symbol_extractor.py`'s language table to Go/Rust** (needed
   for §3.3) is genuinely new scope beyond "materialize files, reuse
   pipeline" — call this out explicitly as the one piece of net-new
   extraction logic in this plan, not a config/wiring exercise.
5. **§3.2a's restore-step trigger (lazy vs. eager)** — recommended lazy
   (on first `resolve_symbol`/Pillar-3 miss), but confirm once used in
   anger whether eager-at-onboarding is actually preferred for predictable
   first-query latency on CI/fresh-clone setups.
6. **§3.2a's persistent restore-cache volume sizing/eviction** — no quota
   proposed yet, same "log growth, size a real quota later from observed
   data" posture as §1.7's external-docs cache. Revisit together once both
   caches have real usage data.

## 5. Phased rollout

> **Status update:** LSP integration (`docs/lsp-integration.md`) and the
> call/import graph + Tier-1 chunking (`docs/architecture-deep-dive.md`) have
> both **shipped** since this plan was written. Phase 4's gate is satisfied —
> the shipped `resolve_symbol`
> tool (`kind: definition|references|hover`) is ready to use now, not a future
> dependency. `manifest_reader.py` (§0.1 correction) is still net-new and not
> shared with anything already shipped.

- **Phase 1**: Pillar 2 (out-of-root local docs) — near-zero effort, ship
  first, validates the `always_tree_paths` extension mechanism. Confirmed
  the underlying mechanism (`always_tree_paths`, `extra_gitignore_container_paths`)
  already exists in `validate_config.py`/`config_loader.py` — no new plumbing
  needed for the bind-mount side, only the config wiring.
- **Phase 2**: Pillar 1 for one ecosystem end-to-end (recommend
  TypeScript/npm docs, since `llms.txt` adoption is already common there) —
  validates crawl → materialize → index → retrieve loop before generalizing
  `source:` handling to arbitrary sites.
- **Phase 3**: Pillar 1 generalized to remaining configured sources +
  `recrawl_on_version_change`.
- **Phase 4a**: TS for Pillar 3 — **ready to start now**, no §3.2a
  dependency (`.d.ts` already sits under `root_path`). Reuses the shipped
  `resolve_symbol` tool's `hover`/`definition` output where useful, but
  needs no restore step and no `symbol_extractor.py` extension.
- **Phase 4b**: §3.2a's restore step (Go/Rust first, since they're next in
  the ecosystem table) — this is genuinely new infrastructure (per-language
  restore command + persistent cache volume), sequence it as its own
  checkpoint before claiming Go/Rust Pillar 3 support, and note it also
  fixes real dependency hover/definition for `resolve_symbol` on those two
  languages today (shared win, do it once).
- **Phase 4c**: Go/Rust for Pillar 3, now that §3.2a exists — extend
  `symbol_extractor.py`'s language table (§3.3) and extract exported/pub
  symbols only. Still needs `manifest_reader.py` built fresh (§0.1
  correction) — that piece was never actually shared with the LSP bridge.
- **Phase 5 (stretch, only if demanded)**: Pillar 3 best-effort for
  Python/C# (needs §3.2a for C#, and for Python without an in-repo venv);
  Java/Kotlin stays excluded absent a concrete need.
