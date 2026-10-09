"""
watcher.py
Compatibility re-export for callers that import run_watcher().

The real implementation has moved into ProjectWatcher in project_watcher.py.
Single-project container entrypoint.py still uses this wrapper during the
consolidation refactor; multi-project mode uses multi_entrypoint.py directly.
"""
import os, logging
from pathlib import Path

import config_loader as cfg
from project_watcher import ProjectWatcher

log = logging.getLogger(__name__)


def run_watcher():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    project = cfg.get_project()
    resolved_path = os.environ.get("RESOLVED_PATH", "/app/.resolved.json")
    import json
    with open(resolved_path) as f:
        resolved = json.load(f)
    root = Path(cfg.project_root())
    watcher = ProjectWatcher(project, resolved, str(root))
    watcher.run()
