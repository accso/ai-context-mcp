"""
db.py
Thin wrapper around PostgreSQL (psycopg2) and Qdrant.

To support the consolidated watcher where multiple projects share one process,
this module no longer uses module-level singletons for the DB connection or
Qdrant client. Instead, DB/Qdrant instances are scoped to the current thread
(via threading.local()). Each ProjectWatcher thread calls
config_loader.set_thread_context(...) before running, which in turn registers
its per-project DB instances here.

Schema (multi-project):
  projects       (project_key PK, display_name, profile, root_path, …)
  file_index     (project_key, relative_path PK, content_sha256, language, chunk_count, …)
  l1_summaries   (project_key, relative_path, content_sha256 PK, summary, …)
  l0_maps        (project_key PK, map_json JSONB, …)
  doc_trees      (project_key, file_path UNIQUE, content_sha256, tree_json JSONB, node_count, …)
"""
from __future__ import annotations

import hashlib, json, uuid, threading
import psycopg2, psycopg2.extras
import config_loader as cfg
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue

# Per-thread DB/client instances. Each ProjectWatcher thread sets these once
# via set_thread_db() and then reuses them for its whole lifetime.
_local = threading.local()

# Legacy module-level fallback (single-project container).
_pg = None
_qd = None


def set_thread_db(pg: psycopg2.extensions.connection, qd: QdrantClient) -> None:
    """Bind this thread to its own PostgreSQL connection and Qdrant client."""
    _local.pg = pg
    _local.qd = qd


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ── Postgres ──────────────────────────────────────────────────────────────────

def pg():
    global _pg
    if hasattr(_local, "pg"):
        pg_conn = _local.pg
        if pg_conn is None or pg_conn.closed:
            pg_conn = psycopg2.connect(cfg.postgres_url())
            pg_conn.autocommit = True
            _local.pg = pg_conn
        return pg_conn
    if _pg is None or _pg.closed:
        _pg = psycopg2.connect(cfg.postgres_url())
        _pg.autocommit = True
    return _pg


# ── Qdrant ────────────────────────────────────────────────────────────────────

def qd() -> QdrantClient:
    global _qd
    if hasattr(_local, "qd"):
        client = _local.qd
        if client is None:
            client = QdrantClient(url=cfg.qdrant_url())
            _local.qd = client
        return client
    if _qd is None:
        _qd = QdrantClient(url=cfg.qdrant_url())
    return _qd


# ── file_index ────────────────────────────────────────────────────────────────

def get_file_record(project_key: str, relative_path: str) -> dict | None:
    with pg().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM file_index WHERE project_key=%s AND relative_path=%s",
            (project_key, relative_path),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def upsert_file_record(project_key: str, relative_path: str, content_sha256: str,
                       language: str, chunk_count: int, chunker_version: int) -> None:
    with pg().cursor() as cur:
        cur.execute(
            """
            INSERT INTO file_index
            (project_key, relative_path, content_sha256, language, chunk_count, chunker_version, last_indexed_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW()) ON CONFLICT (project_key, relative_path) DO
            UPDATE SET
                content_sha256 = EXCLUDED.content_sha256,
                language = EXCLUDED.language,
                chunk_count = EXCLUDED.chunk_count,
                chunker_version = EXCLUDED.chunker_version,
                last_indexed_at = NOW()
            """,
            (project_key, relative_path, content_sha256, language, chunk_count, chunker_version),
        )


def delete_file_record(project_key: str, relative_path: str) -> None:
    with pg().cursor() as cur:
        cur.execute(
            "DELETE FROM file_index WHERE project_key=%s AND relative_path=%s",
            (project_key, relative_path),
        )


def get_all_file_records(project_key: str) -> dict[str, str]:
    """Return {relative_path: content_sha256} for every file in the index for this project."""
    with pg().cursor() as cur:
        cur.execute(
            "SELECT relative_path, content_sha256 FROM file_index WHERE project_key=%s",
            (project_key,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def get_all_file_versions(project_key: str) -> dict[str, tuple[str, int]]:
    """Return {relative_path: (content_sha256, chunker_version)} for every file
    in the index for this project."""
    with pg().cursor() as cur:
        cur.execute(
            "SELECT relative_path, content_sha256, chunker_version FROM file_index WHERE project_key=%s",
            (project_key,),
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def get_all_file_chunk_counts(project_key: str) -> dict[str, int]:
    """Return {relative_path: chunk_count} for every file in the index for this project."""
    with pg().cursor() as cur:
        cur.execute(
            "SELECT relative_path, chunk_count FROM file_index WHERE project_key=%s",
            (project_key,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def get_total_indexed_chunks(project_key: str) -> int:
    """Return SUM(chunk_count) from file_index for the project (0 if empty)."""
    with pg().cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(chunk_count), 0) FROM file_index WHERE project_key=%s",
            (project_key,),
        )
        return int(cur.fetchone()[0])


def get_qdrant_collection_count(project_key: str) -> int:
    """Return the exact total number of vectors stored in the Qdrant collection."""
    try:
        result = qd().count(collection_name=project_key, exact=True)
        return result.count
    except Exception:
        return -1


def count_qdrant_file_chunks(project_key: str, relative_path: str) -> int:
    """Count actual Qdrant vectors stored for a specific file."""
    result = qd().count(
        collection_name=project_key,
        count_filter=Filter(must=[
            FieldCondition(key="project_key", match=MatchValue(value=project_key)),
            FieldCondition(key="relative_path", match=MatchValue(value=relative_path)),
        ]),
        exact=True,
    )
    return result.count


def get_files_missing_l1(project_key: str) -> list[tuple[str, str]]:
    """Return [(relative_path, content_sha256)] for every file_index row
    that has no matching l1_summaries entry for its current SHA."""
    with pg().cursor() as cur:
        cur.execute(
            """
            SELECT fi.relative_path, fi.content_sha256
            FROM file_index fi
                     LEFT JOIN l1_summaries l1
                               ON l1.project_key = fi.project_key
                                   AND l1.relative_path = fi.relative_path
                                   AND l1.content_sha256 = fi.content_sha256
            WHERE fi.project_key = %s
              AND l1.summary IS NULL
            ORDER BY fi.relative_path
            """,
            (project_key,),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


# ── l1_summaries ──────────────────────────────────────────────────────────────

def get_l1_summary(project_key: str, relative_path: str, content_sha256: str) -> str | None:
    """Return cached L1 summary if it exists for this exact content version."""
    with pg().cursor() as cur:
        cur.execute(
            "SELECT summary FROM l1_summaries "
            "WHERE project_key=%s AND relative_path=%s AND content_sha256=%s",
            (project_key, relative_path, content_sha256),
        )
        row = cur.fetchone()
        return row[0] if row else None


def upsert_l1_summary(project_key: str, relative_path: str,
                      content_sha256: str, summary: str) -> None:
    with pg().cursor() as cur:
        cur.execute(
            """
            INSERT INTO l1_summaries
                (project_key, relative_path, content_sha256, summary, generated_at)
            VALUES (%s, %s, %s, %s, NOW()) ON CONFLICT (project_key, relative_path, content_sha256) DO
            UPDATE SET
                summary = EXCLUDED.summary,
                generated_at = NOW()
            """,
            (project_key, relative_path, content_sha256, summary),
        )


# ── l0_maps ───────────────────────────────────────────────────────────────────

def get_l0(project_key: str) -> dict | None:
    with pg().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT map_json FROM l0_maps WHERE project_key=%s", (project_key,))
        row = cur.fetchone()
        return row["map_json"] if row else None


def upsert_l0(project_key: str, map_json: dict) -> None:
    with pg().cursor() as cur:
        cur.execute(
            """
            INSERT INTO l0_maps (project_key, map_json, generated_at)
            VALUES (%s, %s, NOW()) ON CONFLICT (project_key) DO
            UPDATE SET
                map_json = EXCLUDED.map_json,
                generated_at = NOW()
            """,
            (project_key, json.dumps(map_json)),
        )


# ── Qdrant: chunks ────────────────────────────────────────────────────────────

def stable_chunk_id(project_key: str, relative_path: str,
                    chunk_text: str, start_line: int) -> str:
    if chunk_text:
        key = f"{project_key}:{relative_path}:{chunk_text}"
    else:
        key = f"{project_key}:{relative_path}:line{start_line}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def get_chunk_ids_for_file(project_key: str, relative_path: str) -> set[str]:
    """Return the set of stable Qdrant point IDs currently stored for this file."""
    all_ids: set[str] = set()
    offset = None
    while True:
        points, next_offset = qd().scroll(
            collection_name=project_key,
            scroll_filter=Filter(must=[
                FieldCondition(key="project_key", match=MatchValue(value=project_key)),
                FieldCondition(key="relative_path", match=MatchValue(value=relative_path)),
            ]),
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        all_ids.update(str(p.id) for p in points)
        if not next_offset:
            break
        offset = next_offset
    return all_ids


def delete_file_chunks(project_key: str, relative_path: str) -> None:
    """Remove ALL Qdrant points for a given file (full delete — used by remove_file)."""
    qd().delete(
        collection_name=project_key,
        points_selector=Filter(must=[
            FieldCondition(key="relative_path", match=MatchValue(value=relative_path)),
            FieldCondition(key="project_key", match=MatchValue(value=project_key)),
        ]),
    )


def upsert_chunks_delta(
        project_key: str,
        relative_path: str,
        language: str,
        chunks: list[dict],
        vectors: list[list[float]],
        existing_ids: set[str],
) -> tuple[int, int]:
    """
    Delta-upsert: embed and store only new/changed chunks; delete removed ones.
    Returns (added_count, deleted_count).

    stable_chunk_id() is a hash of chunk TEXT only (not start_line/end_line/
    chunk_type/symbol — see its docstring). This means a chunker fix that only
    changes *boundary labeling or classification* (e.g. a tree-sitter rewrite
    that re-derives the same exact text span but now tags it "interface"
    instead of "class", or shifts which start_line a later, byte-identical
    declaration is recorded under) produces the SAME id as before — so those
    points would never be re-upserted (and their stale payload never
    refreshed) if we only handled to_add/to_delete.     Discovered via a real live-data bug during the Tier-1 tree-sitter
    chunk-boundary rewrite (see "Structure-aware chunking" in
    docs/architecture-deep-dive.md): CHUNKER_VERSION bumps correctly forced
    re-chunking, but previously-stored payloads for text-identical chunks kept
    showing pre-fix start_line/end_line/chunk_type values indefinitely.
    Fixed by also refreshing payload (cheap — no re-embedding) for every
    "unchanged" (by id) point on each re-chunk, not just add/delete.
    """
    new_points: dict[str, PointStruct] = {}
    for chunk, vec in zip(chunks, vectors):
        cid = stable_chunk_id(project_key, relative_path,
                              chunk["text"], chunk["start_line"])
        new_points[cid] = PointStruct(
            id=cid,
            vector=vec,
            payload={
                "project_key": project_key,
                "relative_path": relative_path,
                "language": language,
                "text": chunk["text"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "symbol": chunk.get("symbol"),
                "chunk_type": chunk.get("chunk_type", "block"),
            },
        )

    to_add = {cid: p for cid, p in new_points.items() if cid not in existing_ids}
    to_keep = {cid: p for cid, p in new_points.items() if cid in existing_ids}
    to_delete = list(existing_ids - set(new_points.keys()))

    if to_delete:
        qd().delete(
            collection_name=project_key,
            points_selector=to_delete,
        )

    if to_add:
        qd().upsert(collection_name=project_key, points=list(to_add.values()))

    # Refresh payload only (no re-embedding) for text-unchanged points, in
    # case their boundary/classification metadata changed — see docstring.
    for cid, p in to_keep.items():
        qd().set_payload(collection_name=project_key, payload=p.payload, points=[cid])

    return len(to_add), len(to_delete)


def upsert_chunks(project_key: str, relative_path: str, language: str,
                  chunks: list[dict], vectors: list[list[float]]) -> None:
    """Full-replace upsert — deletes existing then inserts all. Kept for compat."""
    delete_file_chunks(project_key, relative_path)
    new_points = [
        PointStruct(
            id=stable_chunk_id(project_key, relative_path,
                               chunk["text"], chunk["start_line"]),
            vector=vec,
            payload={
                "project_key": project_key,
                "relative_path": relative_path,
                "language": language,
                "text": chunk["text"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "symbol": chunk.get("symbol"),
                "chunk_type": chunk.get("chunk_type", "block"),
            },
        )
        for chunk, vec in zip(chunks, vectors)
    ]
    if new_points:
        qd().upsert(collection_name=project_key, points=new_points)


def search_chunks(project_key: str, query_vec: list[float], top_k: int) -> list[dict]:
    results = qd().search(
        collection_name=project_key,
        query_vector=query_vec,
        limit=top_k,
        with_payload=True,
        query_filter=Filter(must=[
            FieldCondition(key="project_key", match=MatchValue(value=project_key)),
        ]),
    )
    return [{"score": r.score, **r.payload} for r in results]


# ── doc_trees ─────────────────────────────────────────────────────────────────

def upsert_doc_tree(project_key: str, file_path: str, sha: str,
                    tree_json: dict, node_count: int) -> None:
    with pg().cursor() as cur:
        cur.execute(
            """
            INSERT INTO doc_trees
            (project_key, file_path, content_sha256, tree_json, node_count, generated_at)
            VALUES (%s, %s, %s, %s, %s, NOW()) ON CONFLICT (project_key, file_path) DO
            UPDATE SET
                content_sha256 = EXCLUDED.content_sha256,
                tree_json = EXCLUDED.tree_json,
                node_count = EXCLUDED.node_count,
                generated_at = NOW()
            """,
            (project_key, file_path, sha, json.dumps(tree_json), node_count),
        )


def get_doc_tree(project_key: str, file_path: str) -> dict | None:
    with pg().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM doc_trees WHERE project_key=%s AND file_path=%s",
            (project_key, file_path),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_doc_tree(project_key: str, file_path: str) -> None:
    with pg().cursor() as cur:
        cur.execute(
            "DELETE FROM doc_trees WHERE project_key=%s AND file_path=%s",
            (project_key, file_path),
        )


def get_doc_trees_for_project(project_key: str) -> list[dict]:
    with pg().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM doc_trees WHERE project_key=%s ORDER BY file_path",
            (project_key,),
        )
        return [dict(row) for row in cur.fetchall()]


def get_doc_tree_sha(project_key: str, file_path: str) -> str | None:
    with pg().cursor() as cur:
        cur.execute(
            "SELECT content_sha256 FROM doc_trees WHERE project_key=%s AND file_path=%s",
            (project_key, file_path),
        )
        row = cur.fetchone()
        return row[0] if row else None


def get_doc_trees_with_null_summaries(project_key: str) -> list[dict]:
    """Return doc_trees rows where any node summary is null (LLM was unavailable)."""
    with pg().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, file_path, content_sha256, tree_json, node_count
            FROM doc_trees
            WHERE project_key = %s
              AND tree_json::text LIKE %s
            ORDER BY file_path
            """,
            (project_key, '%"summary": null%'),
        )
        return [dict(row) for row in cur.fetchall()]


def count_doc_trees(project_key: str) -> int:
    with pg().cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM doc_trees WHERE project_key=%s",
            (project_key,),
        )
        return int(cur.fetchone()[0])


def get_doc_tree_node_total(project_key: str) -> int:
    with pg().cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(node_count), 0) FROM doc_trees WHERE project_key=%s",
            (project_key,),
        )
        return int(cur.fetchone()[0])


# ── symbol_index ──────────────────────────────────────────────────────────────

def upsert_symbols(project_key: str, file_path: str, symbols: list[dict]) -> None:
    """Replace all symbols for a file atomically."""
    with pg().cursor() as cur:
        cur.execute(
            "DELETE FROM symbol_index WHERE project_key=%s AND file_path=%s",
            (project_key, file_path),
        )
    if not symbols:
        return
    with pg().cursor() as cur:
        for sym in symbols:
            cur.execute(
                """
                INSERT INTO symbol_index
                (project_key, file_path, symbol_name, symbol_kind,
                 parent_symbol, signature, start_line, end_line, chunk_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        %s) ON CONFLICT (project_key, file_path, start_line, symbol_name) DO
                UPDATE SET
                    symbol_kind = EXCLUDED.symbol_kind,
                    parent_symbol = EXCLUDED.parent_symbol,
                    signature = EXCLUDED.signature,
                    end_line = EXCLUDED.end_line,
                    chunk_id = EXCLUDED.chunk_id,
                    indexed_at = NOW()
                """,
                (
                    project_key, file_path,
                    sym["symbol_name"], sym["symbol_kind"],
                    sym.get("parent_symbol"), sym.get("signature"),
                    sym["start_line"], sym["end_line"],
                    sym.get("chunk_id"),
                ),
            )


def delete_symbols(project_key: str, file_path: str) -> None:
    with pg().cursor() as cur:
        cur.execute(
            "DELETE FROM symbol_index WHERE project_key=%s AND file_path=%s",
            (project_key, file_path),
        )


def get_symbols_for_file(project_key: str, file_path: str) -> list[dict]:
    """Return id/symbol_name/start_line for every symbol currently stored for
    this file — used right after upsert_symbols() to resolve call-edge
    caller/callee names to symbol_index IDs (upsert_symbols() deletes and
    re-inserts, so IDs change on every reindex; callers must re-fetch)."""
    with pg().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, symbol_name, start_line, end_line
            FROM symbol_index
            WHERE project_key = %s AND file_path = %s
            """,
            (project_key, file_path),
        )
        return [dict(row) for row in cur.fetchall()]


def find_symbol_ids_by_name(project_key: str, symbol_name: str, files: list[str]) -> list[dict]:
    """Find symbol_index rows matching an exact (case-sensitive) name, scoped
    to a specific set of files — used for cross-file call-edge resolution
    (candidates = files reachable via this file's import_edges.resolved_file).
    Returns [] immediately if `files` is empty (avoids an unscoped query)."""
    if not files:
        return []
    with pg().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, file_path, start_line
            FROM symbol_index
            WHERE project_key = %s AND symbol_name = %s AND file_path = ANY(%s)
            """,
            (project_key, symbol_name, files),
        )
        return [dict(row) for row in cur.fetchall()]


# ── call_edges ──────────────────────────────────────────────────────────────

def upsert_call_edges(project_key: str, caller_file: str, edges: list[dict]) -> None:
    """Replace all call edges for a file atomically (delete + re-insert, same
    pattern as upsert_symbols/upsert_import_edges). Each edge dict:
      {"caller_symbol_id": int|None, "callee_name": str,
       "callee_symbol_id": int|None, "resolution": str, "call_line": int}
    """
    with pg().cursor() as cur:
        cur.execute(
            "DELETE FROM call_edges WHERE project_key=%s AND caller_file=%s",
            (project_key, caller_file),
        )
    if not edges:
        return
    with pg().cursor() as cur:
        for edge in edges:
            cur.execute(
                """
                INSERT INTO call_edges
                (project_key, caller_file, caller_symbol_id, callee_name,
                 callee_symbol_id, resolution, call_line)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project_key, caller_file,
                    edge.get("caller_symbol_id"), edge["callee_name"],
                    edge.get("callee_symbol_id"), edge["resolution"],
                    edge["call_line"],
                ),
            )


def delete_call_edges(project_key: str, file_path: str) -> None:
    with pg().cursor() as cur:
        cur.execute(
            "DELETE FROM call_edges WHERE project_key=%s AND caller_file=%s",
            (project_key, file_path),
        )


# ── import_edges ──────────────────────────────────────────────────────────────

def upsert_import_edges(project_key: str, importer_file: str, edges: list[dict]) -> None:
    """Replace all import edges for a file atomically (delete + re-insert, same
    pattern as upsert_symbols)."""
    with pg().cursor() as cur:
        cur.execute(
            "DELETE FROM import_edges WHERE project_key=%s AND importer_file=%s",
            (project_key, importer_file),
        )
    if not edges:
        return
    with pg().cursor() as cur:
        for edge in edges:
            cur.execute(
                """
                INSERT INTO import_edges
                (project_key, importer_file, specifier, resolved_file, import_kind, line)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (project_key, importer_file, specifier, line) DO
                UPDATE SET
                    resolved_file = EXCLUDED.resolved_file,
                    import_kind = EXCLUDED.import_kind,
                    indexed_at = NOW()
                """,
                (
                    project_key, importer_file,
                    edge["specifier"], edge.get("resolved_file"),
                    edge["import_kind"], edge["line"],
                ),
            )


def delete_import_edges(project_key: str, file_path: str) -> None:
    with pg().cursor() as cur:
        cur.execute(
            "DELETE FROM import_edges WHERE project_key=%s AND importer_file=%s",
            (project_key, file_path),
        )


def lookup_symbol(project_key: str, symbol_name: str, limit: int = 10) -> list[dict]:
    """Find symbols by name (case-insensitive partial match)."""
    with pg().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT file_path,
                   symbol_name,
                   symbol_kind,
                   parent_symbol,
                   signature,
                   start_line,
                   end_line,
                   chunk_id
            FROM symbol_index
            WHERE project_key = %s
              AND lower(symbol_name) LIKE lower(%s)
            ORDER BY symbol_name
                LIMIT %s
            """,
            (project_key, f"%{symbol_name}%", limit),
        )
        return [dict(row) for row in cur.fetchall()]
