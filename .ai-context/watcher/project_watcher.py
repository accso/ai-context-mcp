"""
project_watcher.py
Per-project file-system watcher factory.

Each ProjectWatcher instance is fully self-contained: it owns its own
watchdog observers, its own debounce state, its own DB/Qdrant connection (via
the per-thread wiring in db.py), and watches exactly one project root.

Usage:
    watcher = ProjectWatcher(project_config, resolved, project_root)
    asyncio.run(watcher.run())
"""
import os, time, logging, threading, asyncio
from pathlib import Path
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

import config_loader as cfg
import db
import indexer
import chunker
import model_guard

log = logging.getLogger(__name__)


class ProjectWatcher:
    """Long-running watcher for a single project."""

    def __init__(
        self,
        project_config: dict,
        resolved: dict,
        project_root_in_container: str,
        extra_gitignore_prefix: str = "/extra-gitignores",
    ) -> None:
        self._project = project_config
        self._resolved = resolved
        self._project_root = Path(project_root_in_container)
        self._extra_gitignore_prefix = extra_gitignore_prefix
        self._project_key = project_config["name"]
        self._debounce = float(project_config["indexer"].get("debounce_seconds", 2))
        self._thread: threading.Thread | None = None

    # ── Internal per-thread setup ─────────────────────────────────────────────

    def _bind_thread(self) -> None:
        """Set up this thread's project-scoped config and DB/Qdrant clients."""
        cfg.set_thread_context(
            project=self._project,
            resolved=self._resolved,
            project_root=str(self._project_root),
            extra_gitignore_prefix=self._extra_gitignore_prefix,
        )
        pg_conn = db.pg()  # creates per-thread connection via db.py
        qd_client = db.qd()  # creates per-thread Qdrant client via db.py
        db.set_thread_db(pg_conn, qd_client)
        log.info(
            f"[watcher] Thread bound to project={self._project_key} "
            f"root={self._project_root}"
        )

    # ── Startup reconciliation & self-healing ─────────────────────────────────

    def _startup_reconcile(self) -> int:
        """Reconcile on-disk state against the DB at watcher startup."""
        pkey = cfg.project_key()
        root = self._project_root
        log.info(f"[watcher] Startup reconciliation — scanning {root} against index ...")

        on_disk: dict[str, Path] = {}
        try:
            on_disk_files = indexer._eligible_files(root)
            for p in on_disk_files:
                rel = str(p.relative_to(root))
                on_disk[rel] = p
        except Exception as e:
            log.error(f"[watcher] Startup reconciliation scan failed: {e}")
            return 0

        try:
            db_versions: dict[str, tuple[str, int]] = db.get_all_file_versions(pkey)
        except Exception as e:
            log.error(f"[watcher] Could not load DB records for reconciliation: {e}")
            return 0

        new_files      = [rel for rel in on_disk if rel not in db_versions]
        deleted_files  = [rel for rel in db_versions if rel not in on_disk]
        changed_files  = []
        for rel, abs_p in on_disk.items():
            if rel not in db_versions:
                continue
            stored_sha, stored_chunker_version = db_versions[rel]
            try:
                content = abs_p.read_text(encoding="utf-8", errors="replace")
                current_sha = db.sha256(content)
            except Exception:
                continue
            if current_sha != stored_sha or stored_chunker_version != chunker.CHUNKER_VERSION:
                changed_files.append(rel)

        total = len(new_files) + len(deleted_files) + len(changed_files)
        log.info(
            f"[watcher] Reconciliation scan complete — "
            f"new={len(new_files)}  changed={len(changed_files)}  deleted={len(deleted_files)}  "
            f"(total changes to apply: {total})"
        )

        if total == 0:
            log.info("[watcher] Index is up-to-date — no reconciliation needed.")
            return 0

        applied = 0
        for rel in new_files:
            abs_p = on_disk[rel]
            try:
                if indexer.index_file(abs_p, str(root)):
                    log.info(f"[watcher] Reconcile  new: {rel}")
                    applied += 1
            except Exception as e:
                log.error(f"[watcher] Reconcile error (new) {rel}: {e}")

        for rel in changed_files:
            abs_p = on_disk[rel]
            try:
                if indexer.index_file(abs_p, str(root)):
                    log.info(f"[watcher] Reconcile  changed: {rel}")
                    applied += 1
            except Exception as e:
                log.error(f"[watcher] Reconcile error (changed) {rel}: {e}")

        for rel in deleted_files:
            abs_p = root / rel
            try:
                indexer.remove_file(abs_p, str(root))
                log.info(f"[watcher] Reconcile  deleted: {rel}")
                applied += 1
            except Exception as e:
                log.error(f"[watcher] Reconcile error (deleted) {rel}: {e}")

        log.info(f"[watcher] Reconciliation done — {applied}/{total} change(s) applied.")
        return applied

    def _self_heal_qdrant(self, force_per_file: bool = False) -> int:
        """Verify Qdrant vector counts match file_index.chunk_count for every file."""
        pkey = cfg.project_key()
        root = self._project_root

        db_total     = db.get_total_indexed_chunks(pkey)
        qdrant_total = db.get_qdrant_collection_count(pkey)

        if qdrant_total < 0:
            log.warning("[heal] Qdrant collection not reachable — skipping consistency check.")
            return 0

        counts_match = (db_total == qdrant_total)
        if counts_match and not force_per_file:
            log.info(f"[heal] Qdrant parity OK — {db_total} chunks (DB == Qdrant).")
            return 0

        if not counts_match:
            log.warning(
                f"[heal] Qdrant/DB chunk count mismatch — "
                f"file_index.SUM={db_total}  Qdrant.count={qdrant_total} — "
                f"running per-file consistency repair ..."
            )
        else:
            log.info(
                f"[heal] Stale gap detected — running per-file Qdrant consistency check "
                f"({db_total} chunks expected) ..."
            )

        file_counts = db.get_all_file_chunk_counts(pkey)
        healed = 0
        for rel_path, expected in file_counts.items():
            actual = db.count_qdrant_file_chunks(pkey, rel_path)
            if actual == expected:
                continue
            abs_p = root / rel_path
            log.warning(
                f"[heal] Chunk mismatch — {rel_path}  "
                f"expected={expected}  qdrant={actual}"
            )
            try:
                if abs_p.exists():
                    db.delete_file_record(pkey, rel_path)
                    db.delete_file_chunks(pkey, rel_path)
                    if indexer.index_file(abs_p, str(root)):
                        log.info(f"[heal] Re-indexed: {rel_path}")
                        healed += 1
                else:
                    db.delete_file_record(pkey, rel_path)
                    db.delete_file_chunks(pkey, rel_path)
                    log.info(f"[heal] Removed ghost entry: {rel_path}")
                    healed += 1
            except Exception as e:
                log.error(f"[heal] Failed to heal {rel_path}: {e}")

        if healed:
            log.info(f"[heal] Qdrant consistency repair done — {healed} file(s) healed.")
        else:
            log.info("[heal] Qdrant consistency check done — no actionable mismatches found.")
        return healed

    def _refresh_l0(self) -> None:
        """Regenerate the L0 project map after any healing pass that changed the index."""
        try:
            import l0_generator
            log.info("[heal] Regenerating L0 project map ...")
            l0_generator.run_l0()
            log.info("[heal] L0 map updated.")
        except Exception as e:
            log.warning(f"[heal] L0 regeneration failed: {e}")

    def _self_heal_l1_summaries(self) -> int:
        """Backfill any missing L1 file summaries."""
        pkey    = cfg.project_key()
        root    = self._project_root
        missing = db.get_files_missing_l1(pkey)

        if not missing:
            log.info("[heal] L1 summaries: all present — nothing to backfill.")
            return 0

        log.info(f"[heal] L1 summaries: {len(missing)} missing — backfilling ...")
        from llm_summariser import summarise_file
        from indexer import _detect_language

        filled = 0
        for rel_path, content_sha in missing:
            abs_p = root / rel_path
            try:
                content  = abs_p.read_text(encoding="utf-8", errors="replace")
                language = _detect_language(abs_p)
                summary  = summarise_file(rel_path, content, language)
                db.upsert_l1_summary(pkey, rel_path, content_sha, summary)
                log.info(f"[heal] L1 backfilled: {rel_path}")
                filled += 1
            except Exception as e:
                log.warning(f"[heal] L1 backfill failed for {rel_path}: {e}")

        log.info(f"[heal] L1 backfill done — {filled}/{len(missing)} summaries generated.")
        return filled

    # ── Watchdog event handling ───────────────────────────────────────────────

    def _make_handler(self) -> "_DebounceHandler":
        return _DebounceHandler(self._project_root, self._debounce)

    # ── Public long-running entry ─────────────────────────────────────────────

    def run(self) -> None:
        """Blocking long-running watcher for this project."""
        try:
            self._bind_thread()
            root = self._project_root
            debounce = self._debounce

            log.info(
                f"[watcher] Starting — project={cfg.project_key()}  "
                f"root={root}  debounce={debounce}s"
            )

            if not model_guard.assert_models_available(
                {"llm": cfg.llm_config()},
                api_key=cfg.omlx_api_key(),
            ):
                log.warning(
                    f"[watcher] project={cfg.project_key()}: required model(s) not available. "
                    f"Live indexing and reconciliation are disabled until the missing model(s) "
                    f"are loaded. The existing index will be served read-only."
                )

            total_changes = self._startup_reconcile()
            total_changes += self._self_heal_qdrant(force_per_file=False)
            total_changes += self._self_heal_l1_summaries()
            if total_changes > 0:
                self._refresh_l0()

            handler = self._make_handler()
            observer = Observer()
            observer.schedule(handler, str(root), recursive=True)
            observer.start()
            log.info(f"[watcher] Watching {root} recursively for file-system changes ...")

            polling_observer: PollingObserver | None = None
            polling_interval = cfg.polling_fallback_seconds()
            if polling_interval > 0:
                polling_observer = PollingObserver(timeout=polling_interval)
                polling_observer.schedule(handler, str(root), recursive=True)
                polling_observer.start()
                log.warning(
                    f"[watcher] ⚠ Fallback polling observer ACTIVE (interval={polling_interval:.0f}s). "
                    f"This runs ALONGSIDE the primary native file-event observer, never in place of it — "
                    f"it exists because native inotify/FSEvents forwarding is not reliable on every "
                    f"container runtime/bind-mount setup (notably Docker Desktop / Rancher Desktop on "
                    f"macOS). If file changes are already being picked up within ~{debounce:.0f}s, native "
                    f"events are working fine and this fallback is just redundant background polling. "
                    f"If changes are NOT picked up that fast, native events are likely not propagating — "
                    f"see README.md's 'File watching reliability' section for how to check/fix your mount "
                    f"configuration (this fallback keeps the index reasonably current in the meantime, "
                    f"instead of relying solely on the {cfg.reconcile_interval_minutes()}-minute periodic "
                    f"reconciliation). Set indexer.polling_fallback_seconds: 0 in config.yaml to disable."
                )
            else:
                log.info("[watcher] Fallback polling observer disabled (polling_fallback_seconds=0).")

            reconcile_thread: _PeriodicReconcileThread | None = None
            reconcile_interval = cfg.reconcile_interval_minutes()
            if reconcile_interval > 0:
                reconcile_thread = _PeriodicReconcileThread(
                    watcher=self,
                    interval_secs=reconcile_interval * 60,
                )
                reconcile_thread.start()
            else:
                log.info("[reconcile] Periodic reconciliation disabled (reconcile_interval_minutes=0).")

            try:
                while True:
                    handler.flush_pending()
                    time.sleep(0.5)
            except KeyboardInterrupt:
                log.info("[watcher] Ctrl-C received — shutting down ...")
            finally:
                observer.stop()
                observer.join()
                if polling_observer is not None:
                    polling_observer.stop()
                    polling_observer.join()
                if reconcile_thread is not None:
                    reconcile_thread.stop()
                    reconcile_thread.join(timeout=10)
                log.info("[watcher] Stopped.")
        except Exception as e:
            log.exception(f"[watcher] Fatal error in project {self._project_key}: {e}")
            raise


class _DebounceHandler(FileSystemEventHandler):
    def __init__(self, root: Path, debounce: float):
        self._root = root
        self._debounce = debounce
        self._pending: dict[str, tuple[float, bool]] = {}
        self._lock = threading.Lock()
        self._extensions  = set(cfg.include_extensions())
        self._extra_skip  = cfg.extra_skip_paths()
        self._gitignore   = indexer.load_gitignore(self._root)

    def _eligible(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() not in self._extensions:
            return False
        try:
            rel = p.relative_to(self._root)
        except ValueError:
            return True
        return indexer.should_skip(rel, self._gitignore, self._extra_skip) is None

    def _schedule(self, path: str, delete: bool = False):
        if not self._eligible(path):
            return
        with self._lock:
            self._pending[path] = (time.time(), delete)

    def _scan_directory(self, dir_path: str) -> None:
        """Scan a newly created directory and schedule all eligible files inside."""
        try:
            found = 0
            for p in Path(dir_path).rglob("*"):
                if p.is_file():
                    self._schedule(str(p))
                    found += 1
            if found:
                log.info(f"[watcher] New directory detected — scheduled {found} file(s) from {dir_path}")
        except Exception as e:
            log.warning(f"[watcher] Failed to scan new directory {dir_path}: {e}")

    def on_created(self, event):
        if event.is_directory:
            self._scan_directory(event.src_path)
        else:
            self._schedule(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._schedule(event.src_path, delete=True)

    def on_moved(self, event):
        if not event.is_directory:
            self._schedule(event.src_path, delete=True)
            self._schedule(event.dest_path)
        else:
            self._scan_directory(event.dest_path)

    def flush_pending(self) -> bool:
        """Called every 0.5s to process debounced events."""
        now = time.time()
        to_process: dict[str, bool] = {}
        with self._lock:
            for path, (scheduled_at, delete) in list(self._pending.items()):
                if now - scheduled_at >= self._debounce:
                    to_process[path] = delete
                    del self._pending[path]

        had_changes = False
        for path, delete in to_process.items():
            try:
                if delete:
                    rel = Path(path).relative_to(self._root)
                    log.info(f"[watcher] Removing from index: {rel}")
                    indexer.remove_file(path, str(self._root))
                    had_changes = True
                else:
                    changed = indexer.index_file(path, str(self._root))
                    if changed:
                        rel = Path(path).relative_to(self._root)
                        log.info(f"[watcher] Re-indexed: {rel}")
                        had_changes = True
            except Exception as e:
                log.error(f"[watcher] Error processing {path}: {e}")
        return had_changes


class _PeriodicReconcileThread(threading.Thread):
    """Background thread that runs a full delta scan every interval_secs."""

    def __init__(self, watcher: ProjectWatcher, interval_secs: float) -> None:
        super().__init__(name=f"reconcile-{watcher._project_key}", daemon=True)
        self._watcher   = watcher
        self._interval  = interval_secs
        self._stop_evt  = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        # The periodic thread is spawned from ProjectWatcher.run() and dies not
        # inherit that thread's threading.local() context. Re-bind the watcher
        # configuration explicitly before running any indexing operations.
        self._watcher._bind_thread()

        log.info(
            f"[reconcile] Periodic reconciliation thread started — "
            f"interval={self._interval:.0f}s ({self._interval/60:.1f} min)"
        )
        while not self._stop_evt.wait(timeout=self._interval):
            if self._stop_evt.is_set():
                break
            log.info("[reconcile] Running periodic delta scan ...")
            try:
                self._watcher._startup_reconcile()
            except Exception as e:
                log.error(f"[reconcile] Periodic reconciliation failed: {e}")
        log.info("[reconcile] Periodic reconciliation thread stopped.")
