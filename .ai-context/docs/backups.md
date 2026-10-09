# Database Backups — Full Reference

The watcher container automatically maintains a **full, per-project backup** of both the Qdrant vector collection and the PostgreSQL project data. Backups are stored as plain files inside the repository so team members who clone the repo get an up-to-date index for free — no need to run the full initial index.

For the essentials (backup location, triggers, and restore-at-first-run), see [`README.md`](../README.md#database-backups). This doc adds the full configuration reference, manual single-project restore, and the exact backup contents.

## Backup location

```
.ai-context/database-backup/
  my-platform-backend/
    qdrant.snapshot      Binary Qdrant collection snapshot (fast restore, gitignored)
    qdrant.vectors.json  Scroll-export of all vectors + payloads (git baseline for teammates)
    postgres.json        All project rows (projects · file_index · l1_summaries · l0_maps · doc_trees)
    backup_meta.json     Timestamp, file count, chunk count, doc_tree_count, trigger, format
  my-platform-frontend/
    ...
```

Only one of `qdrant.snapshot` / `qdrant.vectors.json` may be present, or both (when `backup_format: "both"` is used). When both are present, restore selects the **newer file by mtime**. `backup_meta.json` is **optional** — used for display only (file count, chunk count, creation date); restore does not depend on it.

## Backup triggers

| Trigger | When it fires |
|---|---|
| **On shutdown** | Fires when pressing Ctrl-C in the `ai-context-up` terminal — runs `ai-context-backup.sh --both` for all projects in parallel |
| **On `ai-context-down`** | Fires when calling `mise run ai-context-down` — runs `ai-context-backup.sh --both` (skip with `--no-backup`) |
| **Manual** | `mise run ai-context-backup` — can be called at any time while the shared infrastructure is running |

## Configuration

All backup settings live in `config.yaml` under `shared` and apply to every project:

```yaml
shared:
  backup_output_dir: ".ai-context/database-backup"  # where per-project backup files are written

  # Backup format for: mise run ai-context-backup
  # Can be overridden per-run with --snapshot / --json / --both flags.
  #
  #   "both"      (default) — snapshot + json vectors
  #                           Commit qdrant.vectors.json + postgres.json to git for teammates.
  #   "snapshot"  — snapshot only (fast, local restore; gitignored)
  #   "json"      — json vectors only (git baseline; no snapshot)
  #
  # Both `ai-context-up` Ctrl-C and `ai-context-down` always use --both.
  backup_format: "both"
```

> Switching `backup_format` between runs is safe. When both `qdrant.snapshot` and `qdrant.vectors.json` are present (e.g. after a `--both` run), restore selects the **newer file by mtime** — so a recent snapshot takes priority over an older JSON export, and vice versa. If only one file is present, that one is used.

## Restore at first-run

When `mise run ai-context-up` encounters a project that is **not yet registered** (e.g. after a `mise run ai-context-reset` or on a fresh clone) AND a restorable backup exists (`postgres.json` + at least one Qdrant file), it offers a restore option.

**Normal backup** (with `backup_meta.json`):
```
  [backup] Backup found for 'my-platform-backend':
           Created : 2026-05-11 09:05:22 UTC
           Content : 312 files, 4,210 chunks
           (newer file will be used for restore)

  Restore from backup instead of full re-index? [Y/n]
```

**Manually provided backup** (no `backup_meta.json`):
```
  [backup] Backup found for 'my-platform-backend':
           JSON    : 2026-05-10 14:32:18 (file mtime)
           Snapshot: 2026-05-11 09:05:22 (file mtime)
           (newer file will be used for restore)

  Restore from backup instead of full re-index? [Y/n]
```

- **Y (default)** — calls `restore.sh`, which restores the Qdrant collection (format selected by mtime: newer file wins) and imports the Postgres data. Takes seconds instead of minutes.
- **n** — runs the normal full re-index as if no backup existed.

> In non-interactive environments (CI pipelines, scripts without a TTY) the prompt is skipped and a full init is always used.

After a restore the watcher starts normally. Any files that changed since the backup was created are detected and re-indexed by the watcher's standard delta-index mechanism. If the backup is significantly old and many files have changed, consider running `mise run ai-context-reset` for a clean full re-index.

## Manual restore for a single project

```bash
# Wipe the project (does NOT delete the backup)
bash .ai-context/scripts/clean.sh my-platform-backend

# Re-run up — the backup prompt will appear
mise run ai-context-up
```

Or call the restore script directly (skips the prompt):

```bash
bash .ai-context/scripts/restore.sh my-platform-backend
bash .ai-context/scripts/start.sh   my-platform-backend
```

## What is backed up

| Component | What | Format |
|---|---|---|
| Qdrant | Collection snapshot (`backup_format: "snapshot"` or `"both"`) | Binary `.snapshot` file |
| Qdrant | Scroll export of all vectors + payloads (`backup_format: "json"` or `"both"`) | Compact JSON — `qdrant.vectors.json` |
| PostgreSQL · `projects` | Project registration row | JSON |
| PostgreSQL · `file_index` | File path, SHA-256, language, chunk count | JSON |
| PostgreSQL · `l1_summaries` | LLM-generated file summaries (content-addressed) | JSON |
| PostgreSQL · `l0_maps` | Static project structure map | JSON |
| PostgreSQL · `doc_trees` | PageIndex-style hierarchical tree per documentation file | JSON |

> L1 summaries are included in the backup. This means a restore also avoids re-running LLM summarisation for every file — the cached summaries are available immediately.

> **Format detection on restore:** `backup_meta.json` is optional and is used only for display (file count, chunk count, creation date). The Qdrant restore format is always determined by file presence and mtime: when both `qdrant.snapshot` and `qdrant.vectors.json` exist, the **newer file wins**; when only one is present, that one is used. This means you can hand a teammate just `qdrant.snapshot` + `postgres.json` (without meta) and restore will work.

> **Known gap — not currently backed up:** `symbol_index`, `import_edges`, and `call_edges` (the tables backing the symbol index, `analyze_impact`, and the `[hub]`/`[bridge]` tags on `repo_structure`) are **not** included in this backup/restore path today. A restored project's watcher will repopulate them on its next full reconciliation pass, but they are empty immediately after restore, before that pass runs. Documented here (and covered by a tripwire test — see `docs/testing.md`) rather than silently left unnoticed.
