"""
backup_restore_step.py — standalone E2E helper script (not a pytest test).
Runs ONE step (backup or restore or wipe) of the real watcher/backup.py
code path against a real, already-indexed disposable Postgres + Qdrant —
invoked once per step by mcp-server/test/e2e/backup_restore.test.ts so the
TS test can inspect DB/Qdrant state directly between steps (before backup,
after wipe, after restore).

Usage: PROJECT_KEY=... PROJECT_ROOT=... POSTGRES_URL=... QDRANT_URL=... \
       BACKUP_DIR=... BACKUP_FORMAT=json STEP={backup|wipe|restore} \
       python3 backup_restore_step.py
"""
import os
import sys

WATCHER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "watcher")
sys.path.insert(0, os.path.abspath(WATCHER_DIR))

import psycopg2
from qdrant_client import QdrantClient

import backup
import config_loader
import db


def main() -> None:
    step = os.environ["STEP"]
    project_key = os.environ["PROJECT_KEY"]
    project_root = os.environ["PROJECT_ROOT"]
    postgres_url = os.environ["POSTGRES_URL"]
    qdrant_url = os.environ["QDRANT_URL"]

    config_loader.set_thread_context(
        project={"name": project_key, "indexer": {}}, resolved={}, project_root=project_root,
    )

    if step == "backup":
        backup.create_full_backup()
        return

    if step == "wipe":
        pg_conn = psycopg2.connect(postgres_url)
        pg_conn.autocommit = True
        with pg_conn.cursor() as cur:
            # Cascades to file_index/l1_summaries/l0_maps/doc_trees/symbol_index/
            # import_edges/call_edges via ON DELETE CASCADE (see infrastructure/init.sql).
            cur.execute("DELETE FROM projects WHERE project_key = %s", (project_key,))
        pg_conn.close()

        qd_client = QdrantClient(url=qdrant_url)
        qd_client.delete_collection(project_key)
        # restore_backup()'s own docstring: "The Qdrant collection must
        # already exist" — recreate it empty before restoring, exactly as
        # scripts/restore.sh's real step 2 does for a fresh project.
        qd_client.create_collection(
            collection_name=project_key,
            vectors_config={"size": 8, "distance": "Cosine"},
        )
        return

    if step == "restore":
        backup.restore_backup()
        return

    raise ValueError(f"Unknown STEP: {step!r} (expected backup|wipe|restore)")


if __name__ == "__main__":
    main()
