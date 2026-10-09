"""
multi_entrypoint.py — AI Context Watcher (multi-project entrypoint)

Loads .resolved.json once, creates one ProjectWatcher per configured project,
and runs all watchers concurrently in one process. Each watcher runs in its
own thread with its own:
  - watchdog Observer (native inotify/FSEvents)
  - optional PollingObserver fallback
  - PostgreSQL connection
  - Qdrant client
  - debounce state / periodic reconcile timer

Per-project isolation is enforced by bind-mounting each project's root_path to
a distinct container path and by threading.local() state in config_loader,
db.py, and embedder.py.

Supervision:
  - If one project's watcher crashes, it is restarted with exponential backoff
    (max 60s), capped at a small number of consecutive failures.
  - The process exits non-zero only if ALL watchers fail.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import config_loader as cfg
from project_watcher import ProjectWatcher

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Silence low-level HTTP request logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


_MAX_RETRIES = 5
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0


def load_resolved(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def mount_path_for_project(container_base: str, project_key: str) -> str:
    """Distinct container path for each project's bind mount."""
    return f"{container_base.rstrip('/')}/{project_key}"


def extra_gitignore_prefix_for_project(container_base: str, project_key: str) -> str:
    """Distinct prefix for each project's extra gitignore bind mounts."""
    return f"{container_base.rstrip('/')}/{project_key}"


async def _run_one(
    project: dict,
    resolved: dict,
    container_base: str,
    gitignore_base: str,
    qdrant_url: str,
    postgres_url: str,
    ready_event: threading.Event | None = None,
) -> None:
    """Run a single ProjectWatcher, retrying on transient failures."""
    project_key = project["name"]
    project_root = mount_path_for_project(container_base, project_key)
    gitignore_prefix = extra_gitignore_prefix_for_project(gitignore_base, project_key)

    failures = 0
    while failures <= _MAX_RETRIES:
        try:
            # Each ProjectWatcher blocks its own thread.
            await asyncio.to_thread(
                _blocking_run,
                project,
                resolved,
                project_root,
                gitignore_prefix,
                qdrant_url,
                postgres_url,
                ready_event,
            )
            # Normal shutdown via KeyboardInterrupt returns here.
            return
        except asyncio.CancelledError:
            log.info(f"[supervisor] Watcher for '{project_key}' cancelled.")
            return
        except Exception as e:
            failures += 1
            backoff = min(_MAX_BACKOFF, _INITIAL_BACKOFF * (2 ** (failures - 1)))
            log.error(
                f"[supervisor] Watcher for '{project_key}' crashed "
                f"(failure {failures}/{_MAX_RETRIES}): {e}"
            )
            log.error(traceback.format_exc())
            if failures <= _MAX_RETRIES:
                log.info(f"[supervisor] Restarting '{project_key}' in {backoff:.1f}s ...")
                await asyncio.sleep(backoff)

    log.error(f"[supervisor] Watcher for '{project_key}' exceeded retry limit — giving up.")
    raise RuntimeError(f"Watcher for '{project_key}' failed {_MAX_RETRIES} consecutive times.")


def _blocking_run(
    project: dict,
    resolved: dict,
    project_root: str,
    gitignore_prefix: str,
    qdrant_url: str,
    postgres_url: str,
    ready_event: threading.Event | None,
) -> None:
    """Thread entry point that starts one ProjectWatcher."""
    # Pre-flight safety assertion: verify the mount path exists. On some macOS
    # container runtimes (Rancher Desktop with virtiofs) bind mounts can take a
    # moment to propagate into the container namespace, so we retry briefly.
    root_path = Path(project_root)
    wait_total = 0.0
    while not root_path.exists() and wait_total < 5.0:
        time.sleep(0.2)
        wait_total += 0.2

    if not root_path.exists():
        parent = root_path.parent
        try:
            parent_listing = os.listdir(parent) if parent.exists() else "<parent missing>"
        except OSError as exc:
            parent_listing = f"<cannot list: {exc}>"
        raise FileNotFoundError(
            f"Project '{project['name']}' mount path does not exist: {root_path}. "
            f"Check the bind-mount configuration for this project. "
            f"/workspace listing: {parent_listing}"
        )

    # Resolve only after we confirmed the path is visible.
    root_path = root_path.resolve()

    # Set environment values expected by legacy accessors during thread bind.
    os.environ["QDRANT_URL"] = qdrant_url
    os.environ["POSTGRES_URL"] = postgres_url
    os.environ["PROJECT_ROOT"] = str(root_path)

    watcher = ProjectWatcher(
        project_config=project,
        resolved=resolved,
        project_root_in_container=str(root_path),
        extra_gitignore_prefix=gitignore_prefix,
    )

    if ready_event:
        ready_event.set()

    watcher.run()


def _assert_mounts_unique(projects: list[dict], container_base: str) -> None:
    """Fail fast if two projects would map to the same container mount path."""
    seen: dict[str, str] = {}
    for p in projects:
        path = mount_path_for_project(container_base, p["name"])
        if path in seen:
            raise ValueError(
                f"Mount collision: projects '{seen[path]}' and '{p['name']}' "
                f"both resolve to {path}. Check config.yaml root_path values."
            )
        seen[path] = p["name"]


def _log_mount_plan(projects: list[dict], resolved: dict, container_base: str) -> None:
    """Human-verifiable startup log of project mount paths."""
    log.info("[startup] Project mount plan:")
    for p in projects:
        root_path = p.get("root_path", "")
        container_path = mount_path_for_project(container_base, p["name"])
        if root_path:
            host_abs = os.path.join(resolved["repo_root"], root_path)
            log.info(f"  [{p['name']}] {host_abs} -> {container_path}")
        else:
            log.info(f"  [{p['name']}] (repo root) -> {container_path}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="AI Context multi-project watcher")
    parser.add_argument("--resolved", default=os.environ.get("RESOLVED_PATH", "/app/.resolved.json"))
    parser.add_argument("--container-root", default="/workspace")
    parser.add_argument("--gitignore-base", default="/extra-gitignores")
    parser.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", "http://localhost:6333"))
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get(
            "POSTGRES_URL",
            "postgresql://ai_context:ai_context_local@localhost:5432/ai_context",
        ),
    )
    args = parser.parse_args()

    resolved = load_resolved(args.resolved)
    projects: list[dict] = list(resolved.get("projects", {}).values())

    if not projects:
        log.error("No projects configured in .resolved.json. Exiting.")
        return 1

    # Ensure parent directories for bind mounts exist up-front. Some container
    # runtimes do not auto-create these before applying volume mounts.
    os.makedirs(args.container_root, exist_ok=True)
    os.makedirs(args.gitignore_base, exist_ok=True)
    log.info(f"[startup] Container root exists: {args.container_root}")
    log.info(f"[startup] Gitignore base exists: {args.gitignore_base}")

    _assert_mounts_unique(projects, args.container_root)
    _log_mount_plan(projects, resolved, args.container_root)

    log.info(f"[startup] Starting {len(projects)} project watcher(s) ...")

    tasks = [
        asyncio.create_task(
            _run_one(
                project=p,
                resolved=resolved,
                container_base=args.container_root,
                gitignore_base=args.gitignore_base,
                qdrant_url=args.qdrant_url,
                postgres_url=args.postgres_url,
            ),
            name=f"watcher-{p['name']}",
        )
        for p in projects
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    failed = [(p["name"], r) for p, r in zip(projects, results) if isinstance(r, Exception)]

    if failed:
        for name, exc in failed:
            log.error(f"[shutdown] Watcher '{name}' terminated with error: {exc}")
        if len(failed) == len(projects):
            log.error("[shutdown] All watchers failed. Exiting with error.")
            return 1
        log.warning("[shutdown] Some watchers failed; others shutdown normally.")
    else:
        log.info("[shutdown] All watchers stopped normally.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        log.info("[main] Ctrl-C received — exiting.")
        sys.exit(0)
