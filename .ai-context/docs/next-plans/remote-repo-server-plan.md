# AI Context — Remote Repository & Server Mode: Implementation Plan

## 0. Goal & Scope

Today, `ai-context` runs **fully local**: the watcher mounts a local directory
read-only, indexes it into Qdrant/PostgreSQL, and an MCP server serves that
index to a locally-running coding agent (Copilot/Crush/JetBrains AI
Assistant). Config is edited by hand in `config.yaml`, and skill/instruction
files are generated onto the local disk by `scripts/init_ide.sh`.

This plan turns `ai-context` into a **server-hosted, multi-repository
service**:

1. Repositories are **remote** (GitHub/GitLab) and are cloned/synced by the
   server itself — no local filesystem mount required.
2. The **watcher concept is preserved conceptually** but re-implemented as a
   **git-sync engine**: it reacts to **provider webhooks** (push events) with
   a **polling fallback**, re-clones/fetches the affected repo, diffs
   changed files, and feeds them through the **exact same
   chunker → embedder → indexer → symbol-extractor → doc-tree pipeline**
   that exists today.
3. A **small web frontend** lets an operator:
   - register a repository (URL, branch, credentials, profile, config knobs),
   - edit the relevant parts of `config.yaml` per project through a form,
   - trigger/inspect indexing status.
   - download the generated `SKILL.md`, an `AGENTS.md` fragment, IDE agent
     rule files (Copilot instructions / Cursor / Windsurf rules), and a
     ready-to-use `mcp.json` snippet for connecting a coding agent to the
     server's MCP endpoint.
4. Operating mode: **single-tenant** (one team/company per deployed
   instance, shared credentials store) — multi-tenancy is explicitly
   out-of-scope for this iteration but the design should not preclude it
   later (see §9).

This document is written so an AI coding agent can implement it directly
against the existing `.ai-context/` codebase without further architectural
decisions. Where a decision was made on the user's behalf, it is marked
**[DECISION]** with the reasoning, so it can be revisited.

---

## 1. High-Level Architecture Change

```
BEFORE (local):
  Local disk (bind mount, read-only)
    → watchdog (inotify/polling)
    → chunker/embedder/indexer
    → Qdrant + PostgreSQL
    → MCP server (agent-facing)

AFTER (server):
  GitHub/GitLab (remote repo)
    → [NEW] Repo Sync Service (git clone/fetch, webhook receiver, poll fallback)
    → local ephemeral working copy on the server (same read-only mount contract,
      just server-managed instead of user-managed)
    → chunker/embedder/indexer/symbol-extractor/doc-tree   (UNCHANGED)
    → Qdrant + PostgreSQL                                   (UNCHANGED, +2 tables)
    → MCP server (agent-facing, now reachable over the network, per project) (mostly unchanged)
    → [NEW] Management API + [NEW] Frontend (register repos, edit config,
      view status, download skill/agents/rules/mcp.json artifacts)
```

**Key design principle:** the indexing pipeline (`watcher/chunker.py`,
`embedder.py`, `indexer.py`, `symbol_extractor.py`, `doc_tree_builder.py`,
`l0_generator.py`, `llm_summariser.py`) stays **untouched**. Only the *trigger
and file-supply layer* changes: instead of `watchdog` observing a bind-mounted
host directory, a new **Repo Sync Service** materializes/updates a working
copy on a volume the watcher container already reads from. This keeps the
blast radius of the change small and testable.

---

## 2. New Component: Repo Sync Service

### 2.1 Responsibilities

- Own the lifecycle of a **remote repository registration**: URL, provider
  (`github` | `gitlab`), branch, credential reference, local mount path,
  associated project name(s) in `config.yaml`.
- Perform the **initial clone** (shallow, `--depth=1` unless the repo needs
  full history for something we don't currently use — we don't, so always
  shallow) into a per-repo working directory:
  `/data/repos/{repo_id}/checkout`.
- On a **webhook push event** or on a **poll tick**, run `git fetch` +
  `git reset --hard origin/{branch}` (safe because we never write to these
  checkouts — read-only from the indexing pipeline's perspective) and compute
  the **changed file set** via `git diff --name-status <old_sha> <new_sha>`.
- Publish changed-file events onto the **same internal signal path** the
  local watcher's debounce logic already uses, so `indexer.py`'s
  `should_skip()` + chunker + embedder pipeline runs unmodified.
- Handle **renames/deletes** using git's own rename detection
  (`git diff -M --name-status`) — maps directly onto the existing
  created/modified/deleted/renamed event vocabulary in `watcher.py`.
- Track **last-synced commit SHA per repo** in PostgreSQL, so a service
  restart resumes correctly (mirrors today's SHA-based reconciliation, just
  keyed on commit SHA in addition to per-file content SHA).

### 2.2 [DECISION] Repository access method

**Chosen: HTTPS clone using per-repository fine-grained credentials**
(GitHub fine-grained PAT / GitLab Project Access Token), stored encrypted,
looked up by the sync service at clone/fetch time. Reasoning:

- Works identically for GitHub and GitLab (the two providers explicitly
  in scope) without needing SSH key distribution/rotation tooling.
- Fine-grained/project tokens can be scoped to `contents: read` only —
  least privilege, easy to revoke per-repo without affecting others.
- Simpler container security model than mounting SSH private keys into
  a container that also runs a network-facing service.
- GitHub Apps are a valid *future* upgrade (see §9) once multi-tenant
  support is needed — deferred because a single-tenant deployment doesn't
  need per-installation OAuth complexity yet.

### 2.3 [DECISION] Change detection

**Chosen: Webhooks (primary) + interval polling (fallback)** — directly
analogous to the existing "native inotify + polling fallback" duality
already documented for the local watcher, and the user explicitly asked to
preserve that concept. Concretely:

- **Webhook receiver** (`POST /webhooks/github`, `POST /webhooks/gitlab`)
  validates the provider signature (`X-Hub-Signature-256` for GitHub,
  `X-Gitlab-Token` for GitLab) against a per-repo webhook secret, extracts
  `ref` + `after` SHA, and if `ref` matches the tracked branch, enqueues an
  immediate sync job for that repo.
- **Polling fallback** (`repo_poll_interval_seconds`, default `120`): a
  scheduler tick per registered repo runs `git ls-remote` (cheap, no clone)
  to compare the remote HEAD SHA against the last-synced SHA; if different,
  enqueues a sync job. This is the network equivalent of
  `polling_fallback_seconds` today and exists for the same reason: webhook
  delivery is not guaranteed (network partitions, misconfigured webhook,
  provider outage, or the webhook simply not registered yet because the
  operator hasn't finished setup). Log a startup warning the same way the
  current watcher does, naming the active interval.
- A **manual "Sync now" action** is additionally exposed in the frontend
  and Management API for operator-triggered re-sync (useful right after
  registering a repo, before/without webhook setup).

### 2.4 New PostgreSQL schema (additive to `init.sql`)

```sql
CREATE TABLE repositories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        TEXT NOT NULL CHECK (provider IN ('github','gitlab')),
    remote_url      TEXT NOT NULL,               -- https clone URL
    branch          TEXT NOT NULL DEFAULT 'main',
    credential_ref  TEXT NOT NULL,                -- key into the secrets store, never the raw token
    checkout_path   TEXT NOT NULL,                -- /data/repos/{id}/checkout
    last_synced_sha TEXT,
    last_synced_at  TIMESTAMPTZ,
    webhook_secret  TEXT NOT NULL,                -- per-repo, used to validate inbound webhook calls
    poll_interval_seconds INT NOT NULL DEFAULT 120,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|cloning|synced|error
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE repository_projects (
    repository_id   UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    project_name    TEXT NOT NULL,                -- matches config.yaml projects[].name
    root_path       TEXT NOT NULL,                -- subpath within checkout, "" = whole repo
    PRIMARY KEY (repository_id, project_name)
);

CREATE TABLE sync_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repository_id   UUID NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    trigger         TEXT NOT NULL,                -- webhook|poll|manual
    from_sha        TEXT,
    to_sha          TEXT,
    files_changed   INT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running', -- running|success|error
    error           TEXT
);
```

One repository can map to **one or more projects** (monorepo case — same
pattern `config.yaml` already supports via multiple `root_path`s).

### 2.5 Credential storage

**[DECISION]** Store tokens encrypted at rest in PostgreSQL using
application-level envelope encryption (AES-256-GCM), with the master key
supplied via `REPO_CREDENTIALS_MASTER_KEY` env var (or a mounted secret
file) — never in `config.yaml` or in the database in plaintext, and never
logged. This mirrors the project's existing convention of keeping secrets
in `.env`/env vars (`OMLX_API_KEY` pattern) rather than in YAML config.
A `secrets.ts`/`secrets.py` module exposes `encrypt(token) -> credential_ref`
and `decrypt(credential_ref) -> token`; `credential_ref` is what's stored in
`repositories.credential_ref` and is an opaque ciphertext blob, not a lookup
key into an external vault (no external vault dependency introduced in this
iteration — acceptable for single-tenant; revisit for multi-tenant, §9).

### 2.6 New service: `repo-sync`

New top-level directory, deployed as its own container, mirroring the
existing `watcher/`, `mcp-server/`, `reranker/` layout:

```
.ai-context/
  repo-sync/
    Dockerfile
    requirements.txt          # gitpython, httpx, fastapi/uvicorn, cryptography
    main.py                   # FastAPI app: webhook receiver + scheduler bootstrap
    git_client.py             # clone/fetch/diff wrapper around GitPython or subprocess git
    scheduler.py              # poll-fallback loop (APScheduler or asyncio loop)
    webhooks.py                # GitHub/GitLab signature validation + payload parsing
    secrets.py                # envelope encryption for tokens
    db.py                     # repositories / repository_projects / sync_events CRUD
    sync_orchestrator.py      # ties clone/fetch+diff -> writes to a "pending changes"
                               # queue consumed by the watcher container's new
                               # "external sync" ingestion mode (see 2.7)
```

### 2.7 Wiring into the existing watcher

**[DECISION]** Rather than duplicating chunker/indexer logic inside
`repo-sync`, the watcher container gains a second ingestion mode alongside
its existing `watchdog` observer:

- `watcher/entrypoint.py` gains a `SYNC_MODE` env var: `filesystem` (today's
  behavior, default for local/dev usage) or `git` (server mode).
- In `git` mode, `watcher.py` does **not** start a `watchdog` Observer.
  Instead it long-polls (or subscribes via a lightweight Postgres
  `LISTEN/NOTIFY` channel — **[DECISION] use `LISTEN/NOTIFY`**, since
  PostgreSQL is already a shared dependency and this avoids adding
  Redis/a message broker purely for a low-throughput signal) a
  `repo_sync_events` channel that `repo-sync` emits on after it has
  fast-forwarded the checkout on disk. The payload is the repo/project id
  plus the list of changed/deleted/renamed relative paths (already computed
  by `git diff --name-status`), which the watcher translates 1:1 into the
  existing `on_created` / `on_modified` / `on_deleted` / `on_moved` handler
  calls it already has — **zero changes needed to chunker.py, embedder.py,
  indexer.py, symbol_extractor.py, doc_tree_builder.py, l0_generator.py**.
- The checkout path (`/data/repos/{id}/checkout/{root_path}`) is mounted
  into the watcher container the same way a local project root is today —
  the only difference is *what* manages the directory's contents (git
  fetch/reset instead of a developer's editor).
- Startup reconciliation (existing SHA-delta scan) still runs on watcher
  boot and is actually *more valuable* now: it naturally covers the case
  where `repo-sync` updated the checkout while the watcher container was
  down.

This is the smallest-diff way to satisfy "the watcher/reranker needs a
concept to hang into the repository to get the information when a change
happens" — the reranker itself needs **no changes at all**, since it only
ever consumes ranked chunk candidates at query time and has no awareness of
where files come from.

---

## 3. Management API (new backend service, or a module in `mcp-server`)

**[DECISION]** New Node/TypeScript service `management-api/`, sibling to
`mcp-server/`, reusing its DB access patterns (`db.ts`) and config loader
conventions, rather than bolting admin routes onto the agent-facing MCP
server — keeps the agent-facing surface (`/mcp`) minimal per the existing
"Minimal manifest" design principle, and keeps operator/config concerns in
one place the frontend talks to exclusively.

### 3.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/repositories` | Register a new repository: `{provider, remote_url, branch, token, poll_interval_seconds}` → creates row, encrypts token, generates webhook secret, kicks off initial clone job |
| `GET` | `/api/repositories` | List registered repositories + status (`pending/cloning/synced/error`), last sync time, last sync SHA |
| `GET` | `/api/repositories/:id` | Detail incl. recent `sync_events` |
| `DELETE` | `/api/repositories/:id` | Deregister (stop syncing; optionally wipe indexed data) |
| `POST` | `/api/repositories/:id/sync` | Manual "Sync now" |
| `GET` | `/api/repositories/:id/webhook-config` | Returns the exact webhook URL + secret + required event types, to paste into GitHub/GitLab settings |
| `POST` | `/api/repositories/:id/projects` | Attach a project definition (`project_name`, `root_path`, `profile`, ports, etc.) — writes into `config.yaml`'s `projects` list (see §4) and `repository_projects` |
| `GET` | `/api/projects/:name/config` | Return the effective merged config (shared + project overrides) for the settings form |
| `PATCH` | `/api/projects/:name/config` | Update the allowed subset of config fields (see §4.2) for a project; validates via the existing `scripts/validate_config.py` logic (exposed as an importable function, not just a CLI) before writing |
| `GET` | `/api/projects/:name/status` | Wraps the existing `get_index_status` MCP-admin tool logic for frontend consumption over plain REST |
| `GET` | `/api/projects/:name/artifacts/skill` | Returns generated `SKILL.md` content (see §5.1) |
| `GET` | `/api/projects/:name/artifacts/agents-md` | Returns generated `AGENTS.md` fragment (see §5.2) |
| `GET` | `/api/projects/:name/artifacts/rules?flavor=copilot\|cursor\|windsurf` | Returns generated IDE rule file(s) (see §5.3) |
| `GET` | `/api/projects/:name/artifacts/mcp-json` | Returns a ready `mcp.json` snippet pointing at this server's `/mcp` endpoint for the project (see §5.4) |
| `GET` | `/api/projects/:name/artifacts/bundle.zip` | All of the above zipped, single download |
| `POST` | `/webhooks/github`, `/webhooks/gitlab` | Proxied to / owned by `repo-sync` (see §2.6) — listed here for completeness of the public surface |

### 3.2 AuthN/AuthZ

**[DECISION]** Since this is single-tenant, protect the entire Management
API (and frontend) with a single shared operator credential to start:
HTTP Basic Auth or a static bearer token set via `MANAGEMENT_API_TOKEN` env
var, checked by middleware. This is intentionally minimal — call out
explicitly in the plan that this is **not** sufficient for multi-tenant or
public-internet exposure without adding real user accounts + RBAC (§9), and
recommend deploying the Management API + frontend behind a VPN/reverse-proxy
with TLS regardless, since it can mint/reveal repo tokens.

The agent-facing `/mcp` endpoints keep whatever auth story is decided for
the network-exposed MCP transport itself (see §7) — separate concern from
the Management API's own auth.

---

## 4. Config Model Changes

### 4.1 `config.yaml` structural additions

The existing `projects[]` schema is preserved as-is (backwards compatible
with local usage). Add one new optional top-level block and one new
optional per-project block:

```yaml
server:
  mode: "server"                     # "local" (default, unchanged behavior) | "server"
  management_api:
    port: 7500
  repo_sync:
    default_poll_interval_seconds: 120
    default_branch: "main"
  data_dir: "/data/repos"            # where checkouts live on the server volume

projects:
  - name: "my-service"
    display_name: "My Service"
    root_path: "services/my-service"     # now interpreted relative to the repo checkout, not local disk
    # profile is auto-detected from the checkout's files, same as local mode — not set here
    repository_id: "<uuid>"              # NEW — links this project to a registered repository row
    mcp:
      port: 7100
    # ...everything else unchanged (doc_index, indexer overrides, sources, skill.target_path, etc.)
```

**[DECISION]** `root_path` keeps its existing meaning (a relative path) —
in local mode it's relative to the repo root on disk; in server mode it's
relative to `data_dir/repos/{repository_id}/checkout`. No field rename
needed, minimizing changes to `config_loader.py`, `doc_tree.ts`, and every
other place that already treats `root_path` as an opaque relative prefix.

### 4.2 Editable-via-frontend field allowlist

Not every config field should be exposed for online editing (some require a
full reset to apply safely, e.g. `indexer.max_chunk_chars` after a chunker
change). Define an explicit allowlist in `management-api/src/config_fields.ts`:

**Safely hot-editable** (apply on next sync, no reset needed):
`profile`, `doc_index.*`, `indexer.extra_skip_paths`,
`indexer.extra_gitignore_paths`, `indexer.debounce_seconds`,
`indexer.reconcile_interval_minutes`, `mcp.max_context_tokens`, `sources`,
`skill.target_path`, `reranker.top_n`.

**Requires re-index warning in the UI** (changing these should prompt "This
requires a full re-index — proceed?", then call the equivalent of
`mise run ai-context-reset` scoped to that project):
`indexer.include_extensions`, `indexer.max_chunk_chars`, `llm.provider/model`.

**Not editable via any config field at all** (embedding model/dimension is
hardcoded in the shared, mandatory inference service, not `config.yaml` —
see [Architecture Deep-Dive — Embedding service](../architecture-deep-dive.md#embedding-service);
`reranker.backend`/`reranker.model` are likewise fixed at Docker build time,
not config.yaml fields): changing either requires rebuilding
`.ai-context/inference/` from source, which is out of scope for this
frontend-editing feature regardless of allowlist design.

**Not editable via frontend at all** (structural/security-sensitive, CLI/
config-file only): `name` (immutable after creation — it's the DB/Qdrant
key), `mcp.port`, `repository_id`.

`scripts/validate_config.py`'s validation logic should be refactored
slightly so it's callable as a library function
(`validate_config(config_dict) -> list[ValidationError]`) from both the CLI
entrypoint (unchanged behavior) and the new `PATCH
/api/projects/:name/config` handler, instead of being CLI-only — avoids
re-implementing validation rules in TypeScript.

---

## 5. Downloadable Artifacts (Frontend Feature)

All four artifact types are **generated on demand** by the Management API
from the project's config + a set of templates, not hand-maintained files —
so they always reflect the current `mcp.port`, `display_name`, and
`profile`.

### 5.1 `SKILL.md`

Already generated today by `scripts/init_ide.sh` onto local disk at
`skill.target_path` (e.g. `.github/skills/{project_name}-context-workflow/SKILL.md`).
**Change:** extract the existing template-filling logic from
`init_ide.sh`/wherever it currently lives into a pure function
(`generate_skill_md(project_config) -> str`) importable by the Management
API, so the CLI script and the new `/artifacts/skill` endpoint share one
source of truth for the template. Content is unchanged (call order,
iteration caps, stop-rule guidance) except that the port/hostname in any
example now points at the **server's** address, not `localhost`.

### 5.2 `AGENTS.md` fragment

**New template.** Produces a Markdown fragment the user pastes into (or the
tool offers to append/merge into) their target repository's `AGENTS.md`:

```markdown
## Repository Context Tool (ai-context)

This repository has an indexed context layer available via MCP at:
`{server_url}/mcp` (project: `{project_name}`).

Before writing code, prefer the following tool call order to orient
yourself instead of grepping or opening files blindly:
1. `repo_structure` — get the project map + directory tree
2. `semantic_search` — find relevant code/docs by identifier or natural language
3. `get_file` — read exact line ranges only after step 2 narrows the target

Stop after at most 2 `semantic_search`/`get_file` round-trips per question
unless the results are genuinely insufficient. Do not dump entire files
into context if a `get_file` line range will do.
```

Endpoint returns this as a standalone fragment (`.md`) plus, in the UI, a
"copy to clipboard" affordance — merging into an existing `AGENTS.md` is a
manual step for the user in this iteration (auto-PR creation is a
reasonable v2, not required now).

### 5.3 IDE / agent rule files

**New templates**, generated per requested `flavor` query param:

| Flavor | Output path convention | Notes |
|---|---|---|
| `copilot` | `.github/copilot-instructions.md` | Same tool-order guidance as §5.2, formatted per Copilot's instructions-file convention |
| `cursor` | `.cursor/rules/ai-context.mdc` | Cursor `.mdc` frontmatter (`description`, `globs: ["**/*"]`, `alwaysApply: true`) + the guidance body |
| `windsurf` | `.windsurf/rules/ai-context.md` | Windsurf rules format |

All three share the same underlying content-generation function as §5.2
(single source of truth for the "how to use the 3 tools" guidance text),
just wrapped in the flavor-specific frontmatter/file convention — implement
as one `renderAgentGuidance(format: "plain"|"copilot"|"cursor"|"windsurf")`
function.

### 5.4 `mcp.json` snippet

```json
{
  "servers": {
    "{project_name}": {
      "type": "http",
      "url": "{server_url}/mcp"
    }
  }
}
```

Match whatever exact `mcp.json` schema the existing local setup already
produces (check `scripts/init_ide.sh` for the current shape and reuse it
verbatim, just substituting `localhost:{port}` → `{server_url}`).

### 5.5 Bundle download

`GET /api/projects/:name/artifacts/bundle.zip` zips all of the above plus a
short `README.txt` explaining where each file goes in the target repo.
Implement with a standard zip library server-side; no new infra needed.

---

## 6. Frontend

**[DECISION]** React + Vite SPA (per explicit preference), served as static
files by the Management API (or a lightweight nginx sidecar) — new
directory `.ai-context/frontend/`.

### 6.1 Screens

1. **Repository list** — table of registered repos (provider, URL, branch,
   status badge, last synced, last error if any), "Add repository" button,
   per-row "Sync now" / "Delete" actions.
2. **Add repository wizard** —
   - Step 1: provider (GitHub/GitLab), clone URL, branch, access token
     (masked input, never redisplayed after save).
   - Step 2: project mapping — one or more `{project_name, root_path,
     profile}` entries (supports monorepo multi-project registration in one
     flow).
   - Step 3: shows the generated **webhook URL + secret** with
     copy-to-clipboard and a link to the provider's exact settings page
     (`https://github.com/{owner}/{repo}/settings/hooks/new` /
     GitLab equivalent) plus the required event type (`push`).
   - On submit: calls `POST /api/repositories` then `POST
     /api/repositories/:id/projects` for each mapped project, kicks off
     initial clone, redirects to the repository detail screen.
3. **Repository/project detail** —
   - Sync history (`sync_events` table rendered as a timeline).
   - Index status panel (file/chunk counts, L1 coverage, Qdrant status,
     freshness) — wraps `GET /api/projects/:name/status`.
   - Config editor: a form (not raw YAML editing) driven by the allowlist
     in §4.2, grouped into "Safe to change" and "Requires re-index"
     sections, with the re-index confirmation dialog for the latter group.
   - Artifacts panel: 4 download buttons (Skill, AGENTS.md, IDE rules
     [with a flavor dropdown], mcp.json) + one "Download all (.zip)" button.
4. **Global settings** (optional, low priority) — shared `embedding`/`llm`/
   `reranker` defaults, mirrors the `shared:` block of `config.yaml`.

### 6.2 Tech choices

- React + Vite + TypeScript, React Router for the 4 screens, a small
  fetch-wrapper (no need for a heavy state library — TanStack Query is a
  reasonable, common, low-risk addition for server-state caching/polling of
  sync status).
- Plain CSS/minimal component library (e.g. a small hand-rolled set or
  Radix primitives) — this is an internal operator tool, not a product
  surface; do not over-invest in visual design here.
- Poll `GET /api/repositories` and `GET /api/projects/:name/status` every
  ~5s while a sync is `running`/`cloning`, stop polling once settled —
  avoids needing a websocket for this iteration.

---

## 7. Networking & Deployment

### 7.1 Container additions to `docker-compose.yml`

New services, following the existing naming convention
(`ai_context_<role>_<project>` for per-project, `ai_context_<role>` for shared):

- `ai_context_repo_sync` (shared, one instance — manages all registered
  repositories; not per-project, since a repo can back multiple projects)
- `ai_context_management_api` (shared, one instance)
- `ai_context_frontend` (shared, one instance, static file server or served
  by `management_api` itself to avoid one extra container)

Per-project `ai_context_watcher_{project}` and `ai_context_mcp_{project}`
remain **per project**, exactly as today, just now mounting
`/data/repos/{repository_id}/checkout/{root_path}` (a server-managed volume)
instead of a bind-mounted host path.

A new shared named volume `ai_context_repo_checkouts` (or a host path under
`/data/repos`) is mounted read-write into `repo-sync` (it needs to
clone/fetch/reset) and read-only into each project's watcher container
(same read-only contract as today's local bind mount).

### 7.2 [DECISION] Deployment target

Left explicitly open per the user's answer, but the plan defaults to
**Docker Compose on a single VM** as the concrete first target, since it is
the smallest change from the existing `infrastructure/docker-compose.yml` +
`start.sh`/`stop.sh` pattern already in the codebase, and everything above
is written against that assumption (named volumes, `docker logs -f
ai_context_...` conventions, etc.). Document a clear extension path to
Kubernetes as a v2 (each per-project pair becomes a `Deployment` +
`Service`, `repo_sync`/`management_api` become singleton `Deployment`s,
checkouts move to a `PersistentVolumeClaim` or ephemeral + re-clone-on-pod-
start) without designing it in detail now — do not build Helm charts in
this iteration.

### 7.3 MCP endpoint exposure

Each project's MCP server (`/mcp`) needs to be reachable from wherever the
coding agent runs (a developer's laptop, potentially outside the server's
private network). Recommend:
- Put a reverse proxy (nginx/Traefik/Caddy) in front of all per-project MCP
  ports, terminating TLS, routing by hostname or path
  (`https://ai-context.internal.example.com/{project_name}/mcp`) instead of
  exposing raw per-project ports externally.
- **[DECISION]** Add a bearer-token check on `/mcp` and `/mcp-admin` (a
  per-project or global static token supplied via env var, checked in
  `mcp-server/src/index.ts` middleware) — today's local setup has no auth
  because the server never left localhost; a network-reachable MCP endpoint
  must not be anonymous-access. `/mcp-admin` should additionally be
  IP-restricted or only reachable via the internal network / VPN, never
  through the public reverse-proxy path, since it's operator-only by
  design already.

---

## 8. Migration & Backwards Compatibility

- `server.mode` defaults to `local` when absent — every existing local
  installation keeps working with zero config changes.
- `watcher/entrypoint.py`'s `SYNC_MODE` env var defaults to `filesystem` —
  same reasoning.
- `scripts/init.sh`/`init_ide.sh`/`start.sh` remain the local-first path;
  add new `scripts/server_init.sh` (or `mise run ai-context-server-up`
  task) for the new mode rather than overloading the existing scripts with
  branching logic — keeps the local dev experience exactly as fast/simple
  as it is today.
- Existing single-project local users who later want to move a project to
  server mode: document a `scripts/migrate_to_server.sh` that (1) registers
  the repo via the Management API using the project's existing git remote,
  (2) reassociates the existing Qdrant collection/PostgreSQL rows by
  project `name` (no re-index needed if the file contents match what's
  already indexed — SHA-based reconciliation on first server-mode sync
  handles this automatically, consistent with existing self-healing logic).

---

## 9. Explicitly Deferred (Out of Scope for This Iteration)

State these clearly so the implementing agent doesn't scope-creep, but the
design above doesn't block them later:

1. **Multi-tenancy** — real user accounts, per-user repo isolation, RBAC.
   Would require: a `users`/`teams` table, scoping every Management API
   query by tenant, per-tenant Qdrant collection namespacing (already
   naturally per-project, so mostly a matter of adding a tenant_id
   dimension), and replacing the single shared `MANAGEMENT_API_TOKEN` with
   real session auth (e.g. OAuth against the git provider itself, which
   also naturally solves "which repos can this user register" via the
   provider's own permission model).
2. **GitHub App / GitLab OAuth App installation flow** instead of manual
   PAT entry — nicer UX, avoids operators pasting long-lived tokens, but
   meaningfully more implementation work (installation callback handling,
   app-level JWT signing, installation token refresh). Recommended as the
   first thing to build once multi-tenancy is in scope, since per-user
   OAuth is required at that point anyway.
3. **Automatic PR creation** for `AGENTS.md`/rules files into the target
   repository (instead of manual download+paste).
4. **Websocket/live-updating** frontend instead of polling.
5. **Kubernetes manifests/Helm chart.**
6. **Full git history access** (currently deliberately shallow-clone only,
   since the pipeline only ever needs working-tree content, not history).

---

## 10. Implementation Order (Suggested Milestones)

1. **DB migrations**: add `repositories`, `repository_projects`,
   `sync_events` tables to `init.sql`; write `db.py`/`db.ts` helpers.
2. **`repo-sync` service, filesystem-writing part only**: clone/fetch/reset
   + `git diff --name-status`, no webhook/poll triggers yet — trigger via a
   manual CLI script for now. Validate that the watcher's `git` `SYNC_MODE`
   correctly reacts to `LISTEN/NOTIFY` events and produces identical
   indexing results to `filesystem` mode on the same content.
3. **Webhook receiver + poll fallback** in `repo-sync`.
4. **Secrets/credential encryption module** + wire into repo registration.
5. **Management API** core endpoints (§3.1) — repository CRUD, manual sync,
   status passthrough.
6. **Config editor endpoints** (`GET/PATCH /api/projects/:name/config`)
   with the allowlist + validation-library refactor.
7. **Artifact generation** (§5) — extract shared template functions, wire
   the 5 download endpoints + zip bundle.
8. **Frontend** — repository list → add wizard → detail/status → config
   editor → artifacts panel, in that order.
9. **Reverse proxy + MCP auth token** for network exposure.
10. **`docker-compose.yml` wiring**, `scripts/server_init.sh`, README/docs
    updates documenting the two modes side by side.

---

## 11. Summary of File-Level Changes

| File/Dir | Change |
|---|---|
| `.ai-context/infrastructure/init.sql` | + `repositories`, `repository_projects`, `sync_events` tables |
| `.ai-context/infrastructure/docker-compose.yml` | + `repo_sync`, `management_api`, `frontend` services; + shared checkout volume |
| `.ai-context/repo-sync/` | **new** service (git client, webhook receiver, poll scheduler, secrets, DB access) |
| `.ai-context/management-api/` | **new** service (REST API for repos/config/artifacts, reuses `mcp-server`'s DB/config conventions) |
| `.ai-context/frontend/` | **new** React+Vite SPA |
| `.ai-context/watcher/entrypoint.py` | + `SYNC_MODE` env var branching |
| `.ai-context/watcher/watcher.py` | + `git` mode: Postgres `LISTEN/NOTIFY` consumer feeding existing event handlers, instead of `watchdog` Observer |
| `.ai-context/watcher/config_loader.py` | interpret `root_path` relative to server checkout dir when `server.mode: server` |
| `.ai-context/scripts/validate_config.py` | refactor core validation into an importable function |
| `.ai-context/scripts/init_ide.sh` | extract skill/mcp.json template generation into shared functions reused by `management-api` |
| `.ai-context/config.yaml` schema | + optional `server:` block; + optional `projects[].repository_id` |
| `.ai-context/scripts/server_init.sh` | **new** — bootstraps server-mode stack |
| `mcp-server/src/index.ts` | + bearer-token auth middleware for network exposure |
| `README.md` / new `docs/server-mode.md` | document local vs. server mode side by side |

---

This plan keeps the existing local, fully-offline workflow completely
intact and additive, while introducing a parallel **server mode** that
replaces "local filesystem + watchdog" with "remote git repository +
webhook/poll-driven sync" as the only structural change to the ingestion
side — everything downstream (chunking, embedding, symbol extraction,
doc-tree building, ranking, MCP serving) is reused unchanged, and a new
Management API + React frontend provide repository registration, scoped
config editing, and on-demand generation/download of `SKILL.md`,
`AGENTS.md`, IDE agent rule files, and `mcp.json` client configuration.
