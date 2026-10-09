"""
backup.py
Per-project database backup and restore.

Backup layout on the host (mounted into the container at /backup):
  .ai-context/database-backup/{project_key}/
    qdrant.snapshot      Qdrant collection snapshot (binary)  — written by shutdown backup
    qdrant.vectors.json  Scroll-export of all vectors+payloads — written by JSON export task
    postgres.json        All rows for this project across all 4 tables (JSON)
    backup_meta.json     Local-only display metadata (created_at, file/chunk counts, trigger)
                         NOT committed to git — used only for CLI prompt display.

Two backup operations — both run together on every backup
──────────────────────────────────────────────────────────
1. Shutdown backup  (Ctrl-C / watcher stop, MODE=backup, BACKUP_FORMAT=both)
   Writes qdrant.snapshot + qdrant.vectors.json + postgres.json.
   Snapshot: fast, atomic, preserves HNSW — binary/large → gitignored.
   JSON:     full vector scroll — text/smaller → tracked in git for sharing.

2. Manual backup  (mise run ai-context-backup)
   Same as above (--both by default).  Pass --snapshot or --json to restrict the format.

Restore format selection — newer file wins:
  Both present → compare mtime → use the more recent one
  One present  → use that one
  None present → fail with a clear error message

Qdrant format "snapshot":
  Restore: POST /collections/{key}/snapshots/upload → wait for green
  ✅ Atomic  ✅ HNSW index preserved (fast first query)  ❌ Large binary, gitignored

Qdrant format "json":
  Restore: PUT /collections/{key}/points (batched upsert) → wait for green
  ✅ Small text  ✅ Git-friendly  ❌ Qdrant rebuilds HNSW on restore (~10–30 s extra)

PostgreSQL workflow (same for both):
  SELECT * WHERE project_key = ...   per-table export to postgres.json
  Restore: DELETE + INSERT per table (full transaction)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg2
import psycopg2.extras
import config_loader as cfg

log = logging.getLogger(__name__)

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backup"))

# Qdrant file names
_SNAP_FILE    = "qdrant.snapshot"
_JSON_FILE    = "qdrant.vectors.json"
_UPSERT_BATCH = 100   # points per upsert call for json restore

# Tables to export/restore, in FK-safe insertion order.
_TABLES = ["projects", "file_index", "l1_summaries", "l0_maps", "doc_trees"]


# ── Public API ────────────────────────────────────────────────────────────────

def backup_exists() -> bool:
    """Return True if a restorable backup is available.

    Requires postgres.json + at least one Qdrant file (snapshot or json).
    backup_meta.json is optional display metadata and not required for restore.
    """
    if not (BACKUP_DIR / "postgres.json").exists():
        return False
    return (BACKUP_DIR / _SNAP_FILE).exists() or (BACKUP_DIR / _JSON_FILE).exists()


def _detect_qdrant_format() -> str:
    """Pick which Qdrant file to restore from.

    When both files are present the NEWER one wins (by file mtime).
    This means:
      - After a shutdown backup  → snapshot is newer  → fast local restore
      - After a JSON git export  → json is newer      → git-baseline restore
      - Fresh clone (json only)  → json is used       → git-baseline restore
      - Only snapshot present    → snapshot is used

    Returns "snapshot" or "json".  If neither file exists returns "snapshot"
    so the caller gets a clear 'file not found' error from the restore step.
    """
    snap      = BACKUP_DIR / _SNAP_FILE
    json_file = BACKUP_DIR / _JSON_FILE
    snap_exists = snap.exists()
    json_exists = json_file.exists()

    if snap_exists and json_exists:
        snap_mtime = snap.stat().st_mtime
        json_mtime = json_file.stat().st_mtime
        if snap_mtime >= json_mtime:
            log.info("[restore] Both Qdrant files present — snapshot is newer, using it (fast restore).")
            return "snapshot"
        else:
            log.info("[restore] Both Qdrant files present — JSON export is newer, using it.")
            return "json"
    if snap_exists:
        return "snapshot"
    if json_exists:
        log.info("[restore] No snapshot found — using JSON vectors file (git-baseline restore).")
        return "json"
    log.warning("[restore] No Qdrant backup file found.")
    return "snapshot"   # will fail with a clear error in the caller


def read_meta() -> dict | None:
    """Read backup_meta.json for the current project. Returns None if not found."""
    p = BACKUP_DIR / "backup_meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def create_full_backup() -> None:
    """Manual backup — format controlled by BACKUP_FORMAT env var.

    BACKUP_FORMAT=both     (default) writes snapshot + json + postgres
    BACKUP_FORMAT=snapshot           writes snapshot + postgres  (fast, local restore)
    BACKUP_FORMAT=json               writes json + postgres      (git baseline)

    Called via MODE=backup from ai-context-backup.sh.
    All projects are launched in parallel containers by the calling script.
    """
    pkey       = cfg.project_key()
    qdrant_url = cfg.qdrant_url()
    fmt        = os.environ.get("BACKUP_FORMAT", "both")
    if fmt not in ("both", "snapshot", "json"):
        log.warning(f"[backup] Unknown BACKUP_FORMAT '{fmt}', falling back to 'both'.")
        fmt = "both"

    log.info(f"[backup] Starting backup for '{pkey}' (format={fmt}) ...")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    snap_tmp = json_tmp = pg_tmp = meta_tmp = None

    # ── Qdrant snapshot ────────────────────────────────────────────────────────
    if fmt in ("both", "snapshot"):
        snap_tmp = BACKUP_DIR / "qdrant.tmp.snapshot"
        snap_tmp.unlink(missing_ok=True)
        snapshot_name = _create_qdrant_snapshot(pkey, qdrant_url)
        try:
            _download_qdrant_snapshot(pkey, qdrant_url, snapshot_name, snap_tmp)
        finally:
            _delete_qdrant_snapshot(pkey, qdrant_url, snapshot_name)

    # ── Qdrant JSON scroll ─────────────────────────────────────────────────────
    if fmt in ("both", "json"):
        json_tmp = BACKUP_DIR / "qdrant.tmp.vectors.json"
        json_tmp.unlink(missing_ok=True)
        _export_qdrant_json(pkey, qdrant_url, json_tmp)

    # ── PostgreSQL ─────────────────────────────────────────────────────────────
    pg_tmp = BACKUP_DIR / "postgres.tmp.json"
    file_count, chunk_count, doc_tree_count = _export_postgres(pkey, pg_tmp)

    # ── Metadata (local display only, gitignored) ──────────────────────────────
    meta = {
        "project_key": pkey,
        "created_at":  datetime.now(timezone.utc).isoformat(),
        "file_count":  file_count,
        "chunk_count": int(chunk_count),
        "doc_tree_count": doc_tree_count,
        "trigger":     "manual",
        "format":      fmt,
    }
    meta_tmp = BACKUP_DIR / "backup_meta.tmp.json"
    meta_tmp.write_text(json.dumps(meta, indent=2))

    # ── Atomic rename ──────────────────────────────────────────────────────────
    if snap_tmp:
        snap_tmp.rename(BACKUP_DIR / _SNAP_FILE)
    if json_tmp:
        json_tmp.rename(BACKUP_DIR / _JSON_FILE)
    pg_tmp.rename(BACKUP_DIR / "postgres.json")
    meta_tmp.rename(BACKUP_DIR / "backup_meta.json")

    sizes = []
    if snap_tmp:
        sizes.append(f"snapshot: {_human_size((BACKUP_DIR / _SNAP_FILE).stat().st_size)}")
    if json_tmp:
        sizes.append(f"json: {_human_size((BACKUP_DIR / _JSON_FILE).stat().st_size)}")
    sizes.append(f"pg: {_human_size((BACKUP_DIR / 'postgres.json').stat().st_size)}")
    log.info(f"[backup] Done — {file_count} files, {chunk_count} chunks  ({', '.join(sizes)})")
    if fmt in ("both", "json"):
        log.info(
            f"[backup] Commit .ai-context/database-backup/{pkey}/ to git "
            f"(qdrant.vectors.json + postgres.json) to share the baseline."
        )


def restore_backup() -> None:
    """
    Restore this project from the available backup.
    The Qdrant collection must already exist (created by restore.sh step 2).

    backup_meta.json is OPTIONAL. When absent the Qdrant format is auto-detected
    from whichever Qdrant file is present (qdrant.vectors.json → "json",
    qdrant.snapshot → "snapshot").
    """
    pkey       = cfg.project_key()
    qdrant_url = cfg.qdrant_url()

    if not backup_exists():
        raise RuntimeError(f"No complete backup found at {BACKUP_DIR}")

    meta       = read_meta() or {}
    qdrant_fmt = _detect_qdrant_format()

    if meta:
        log.info(
            f"[restore] Restoring '{pkey}' from backup  "
            f"(created {meta.get('created_at', 'unknown')}, "
            f"{meta.get('file_count', '?')} files, {meta.get('chunk_count', '?')} chunks, "
            f"qdrant_format={qdrant_fmt}) ..."
        )
    else:
        log.info(
            f"[restore] Restoring '{pkey}' — no backup_meta.json found, "
            f"proceeding with auto-detected format='{qdrant_fmt}' ..."
        )

    if qdrant_fmt == "json":
        _import_qdrant_json(pkey, qdrant_url)
    else:
        _upload_qdrant_snapshot(pkey, qdrant_url)

    _import_postgres(pkey)
    log.info(f"[restore] Restore complete for '{pkey}'.")


# ── Qdrant snapshot helpers (format: "snapshot") ──────────────────────────────

def _create_qdrant_snapshot(pkey: str, base_url: str) -> str:
    r = httpx.post(
        f"{base_url}/collections/{pkey}/snapshots",
        timeout=120.0,
    )
    r.raise_for_status()
    name: str = r.json()["result"]["name"]
    log.info(f"[backup] Qdrant snapshot created: {name}")
    return name


def _download_qdrant_snapshot(pkey: str, base_url: str, name: str, dest: Path) -> None:
    with httpx.stream(
        "GET",
        f"{base_url}/collections/{pkey}/snapshots/{name}",
        timeout=300.0,
    ) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
    log.info(f"[backup] Snapshot downloaded ({_human_size(dest.stat().st_size)})")


def _delete_qdrant_snapshot(pkey: str, base_url: str, name: str) -> None:
    try:
        r = httpx.delete(
            f"{base_url}/collections/{pkey}/snapshots/{name}",
            timeout=30.0,
        )
        if r.status_code not in (200, 404):
            log.warning(f"[backup] Could not delete in-container snapshot '{name}': HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"[backup] Failed to delete in-container snapshot '{name}': {e}")


def _upload_qdrant_snapshot(pkey: str, base_url: str) -> None:
    snap = BACKUP_DIR / _SNAP_FILE
    log.info(f"[restore] Uploading Qdrant snapshot ({_human_size(snap.stat().st_size)}) ...")
    with open(snap, "rb") as f:
        r = httpx.post(
            f"{base_url}/collections/{pkey}/snapshots/upload",
            params={"priority": "snapshot"},
            files={"snapshot": (_SNAP_FILE, f, "application/octet-stream")},
            timeout=300.0,
        )
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"Qdrant snapshot upload failed: HTTP {r.status_code} — {r.text[:200]}"
        )
    log.info("[restore] Qdrant snapshot uploaded. Waiting for collection to be ready ...")
    _wait_for_green(pkey, base_url)


# ── Qdrant JSON helpers (format: "json") ─────────────────────────────────────

def _export_qdrant_json(pkey: str, base_url: str, dest: Path) -> int:
    """
    Scroll all points from Qdrant and write as compact JSON.
    Returns the total number of points exported.

    NOTE: Not atomic. A concurrent index write during the scroll may cause a
    point to appear twice or be missing in the export. For a dev tool this is
    acceptable; switch to format "snapshot" if consistency is critical.
    """
    points: list[dict] = []
    offset = None
    page = 0
    while True:
        body: dict[str, Any] = {
            "limit":        _UPSERT_BATCH,
            "with_vectors": True,
            "with_payload": True,
        }
        if offset is not None:
            body["offset"] = offset

        r = httpx.post(
            f"{base_url}/collections/{pkey}/points/scroll",
            json=body,
            timeout=120.0,
        )
        r.raise_for_status()
        result     = r.json()["result"]
        batch      = result.get("points", [])
        next_off   = result.get("next_page_offset")

        # Keep only the fields needed for upsert
        for pt in batch:
            points.append({
                "id":      pt["id"],
                "vector":  pt["vector"],
                "payload": pt.get("payload", {}),
            })

        page += 1
        if page % 5 == 0:
            log.info(f"[backup] Qdrant JSON scroll: {len(points)} points so far ...")

        if next_off is None:
            break
        offset = next_off

    # Compact JSON (no indent) — vectors are already large enough
    dest.write_text(json.dumps({"points": points}, separators=(",", ":")))
    log.info(f"[backup] Qdrant JSON exported: {len(points)} points ({_human_size(dest.stat().st_size)})")
    return len(points)


def _import_qdrant_json(pkey: str, base_url: str) -> None:
    """
    Upsert all points from qdrant.vectors.json back into Qdrant.
    Qdrant will rebuild its HNSW index asynchronously after the upsert completes.
    """
    vec_file = BACKUP_DIR / _JSON_FILE
    log.info(f"[restore] Loading Qdrant JSON backup ({_human_size(vec_file.stat().st_size)}) ...")
    data   = json.loads(vec_file.read_text())
    points = data.get("points", [])
    total  = len(points)

    for start in range(0, total, _UPSERT_BATCH):
        batch = points[start : start + _UPSERT_BATCH]
        r = httpx.put(
            f"{base_url}/collections/{pkey}/points",
            params={"wait": "true"},
            json={"points": batch},
            timeout=120.0,
        )
        if r.status_code not in (200, 201, 202):
            raise RuntimeError(
                f"Qdrant upsert failed at offset {start}: HTTP {r.status_code} — {r.text[:200]}"
            )
        log.info(f"[restore] Upserted {min(start + _UPSERT_BATCH, total)}/{total} points ...")

    log.info(f"[restore] Qdrant JSON restored: {total} points. Waiting for collection to be ready ...")
    _wait_for_green(pkey, base_url)


# ── Shared Qdrant helpers ─────────────────────────────────────────────────────

def _wait_for_green(pkey: str, base_url: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/collections/{pkey}", timeout=10.0)
            if r.status_code == 200:
                status = r.json().get("result", {}).get("status")
                if status == "green":
                    log.info(f"[restore] Qdrant collection '{pkey}' is green.")
                    return
        except Exception:
            pass
        time.sleep(2)
    log.warning(
        f"[restore] Collection '{pkey}' did not reach green within {timeout}s — continuing anyway."
    )


# ── PostgreSQL helpers ────────────────────────────────────────────────────────

def _export_postgres(pkey: str, dest: Path) -> tuple[int, int, int]:
    """Export per-project rows to dest (JSON). Returns (file_count, chunk_count, doc_tree_count)."""
    conn = psycopg2.connect(cfg.postgres_url())
    conn.autocommit = True
    data: dict[str, list[dict[str, Any]]] = {}
    file_count = 0
    chunk_count = 0
    doc_tree_count = 0
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for table in _TABLES:
                cur.execute(
                    f"SELECT * FROM {table} WHERE project_key = %s", (pkey,)
                )
                data[table] = [dict(row) for row in cur.fetchall()]
            cur.execute("SELECT COUNT(*) FROM file_index WHERE project_key = %s", (pkey,))
            file_count = cur.fetchone()["count"]  # type: ignore[index]
            cur.execute(
                "SELECT COALESCE(SUM(chunk_count), 0) FROM file_index WHERE project_key = %s",
                (pkey,),
            )
            chunk_count = cur.fetchone()["coalesce"]  # type: ignore[index]
            cur.execute("SELECT COUNT(*) FROM doc_trees WHERE project_key = %s", (pkey,))
            doc_tree_count = cur.fetchone()["count"]  # type: ignore[index]
    finally:
        conn.close()

    def _serial(o: Any) -> Any:
        if hasattr(o, "isoformat"):
            return o.isoformat()
        raise TypeError(f"Not serialisable: {type(o)}")

    dest.write_text(json.dumps(data, default=_serial, indent=2))
    total_rows = sum(len(v) for v in data.values())
    log.info(f"[backup] PostgreSQL exported: {total_rows} rows")
    return int(file_count), int(chunk_count), int(doc_tree_count)


def _import_postgres(pkey: str) -> None:
    """Re-insert Postgres rows from backup. Runs as a single transaction."""
    raw: dict[str, list[dict]] = json.loads((BACKUP_DIR / "postgres.json").read_text())

    conn = psycopg2.connect(cfg.postgres_url())
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # Delete in reverse FK order
            cur.execute("DELETE FROM l1_summaries WHERE project_key = %s", (pkey,))
            cur.execute("DELETE FROM l0_maps      WHERE project_key = %s", (pkey,))
            cur.execute("DELETE FROM file_index   WHERE project_key = %s", (pkey,))
            cur.execute("DELETE FROM doc_trees    WHERE project_key = %s", (pkey,))
            cur.execute("DELETE FROM projects     WHERE project_key = %s", (pkey,))

            for row in raw.get("projects", []):
                cur.execute(
                    """
                    INSERT INTO projects
                        (project_key, display_name, profile, root_path,
                         registered_at, last_indexed_at)
                    VALUES (%(project_key)s, %(display_name)s, %(profile)s, %(root_path)s,
                            %(registered_at)s, %(last_indexed_at)s)
                    ON CONFLICT (project_key) DO NOTHING
                    """,
                    row,
                )

            for row in raw.get("file_index", []):
                cur.execute(
                    """
                    INSERT INTO file_index
                        (project_key, relative_path, content_sha256, language,
                         chunk_count, last_indexed_at)
                    VALUES (%(project_key)s, %(relative_path)s, %(content_sha256)s,
                            %(language)s, %(chunk_count)s, %(last_indexed_at)s)
                    ON CONFLICT (project_key, relative_path) DO NOTHING
                    """,
                    row,
                )

            for row in raw.get("l1_summaries", []):
                cur.execute(
                    """
                    INSERT INTO l1_summaries
                        (project_key, relative_path, content_sha256, summary, generated_at)
                    VALUES (%(project_key)s, %(relative_path)s, %(content_sha256)s,
                            %(summary)s, %(generated_at)s)
                    ON CONFLICT (project_key, relative_path, content_sha256) DO NOTHING
                    """,
                    row,
                )

            for row in raw.get("l0_maps", []):
                cur.execute(
                    """
                    INSERT INTO l0_maps (project_key, map_json, generated_at)
                    VALUES (%(project_key)s, %(map_json)s::jsonb, %(generated_at)s)
                    ON CONFLICT (project_key) DO NOTHING
                    """,
                    {**row, "map_json": json.dumps(row["map_json"])},
                )

            for row in raw.get("doc_trees", []):
                cur.execute(
                    """
                    INSERT INTO doc_trees
                        (project_key, file_path, content_sha256, tree_json, node_count, generated_at)
                    VALUES (%(project_key)s, %(file_path)s, %(content_sha256)s,
                            %(tree_json)s::jsonb, %(node_count)s, %(generated_at)s)
                    ON CONFLICT (project_key, file_path) DO NOTHING
                    """,
                    {**row, "tree_json": json.dumps(row["tree_json"]) if isinstance(row["tree_json"], dict) else row["tree_json"]},
                )

        conn.commit()
        total_rows = sum(len(v) for v in raw.values())
        log.info(f"[restore] PostgreSQL imported: {total_rows} rows")
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"

