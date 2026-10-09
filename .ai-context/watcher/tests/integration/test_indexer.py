"""
Integration tests for indexer.py's delta-upsert and remove_file behavior,
against a REAL disposable Postgres + Qdrant (see conftest.py) — not mocks
for the DB/vector-store layer itself, only for the external embedding/LLM
services (embedder.embed_texts, llm_summariser.summarise_file), which are
genuinely separate concerns from "did the delta-upsert/self-heal logic do
the right thing against the real schema".
"""
import config_loader
import db
import indexer


FAKE_VECTOR_DIM = 8


def _fake_embed_texts(texts: list[str]) -> list[list[float]]:
    # Deterministic per-text fake vector (not semantically meaningful — Tier 2
    # only cares that delta-upsert/self-heal move the right rows, not that
    # embeddings are semantically correct, which is the embedding service's
    # own concern).
    return [[float(len(t) % 7 + i) for i in range(FAKE_VECTOR_DIM)] for t in texts]


def _fake_summarise_file(rel_path: str, content: str, language: str) -> str:
    return f"fake summary of {rel_path}"


def test_index_file_writes_chunks_symbols_and_file_record(monkeypatch, pg_conn, qdrant_client, clean_project, tmp_path):
    monkeypatch.setattr("embedder.embed_texts", _fake_embed_texts)
    monkeypatch.setattr("indexer.summarise_file", _fake_summarise_file)
    config_loader.set_thread_context(
        project={"name": clean_project, "indexer": {}}, resolved={}, project_root=str(tmp_path),
    )
    db.set_thread_db(pg_conn, qdrant_client)

    src = tmp_path / "dispatcher.py"
    src.write_text(
        "def route(x):\n"
        "    return handle(x)\n\n"
        "def handle(x):\n"
        "    return x\n"
    )

    indexed = indexer.index_file(src, project_root=str(tmp_path))
    assert indexed is True

    file_record = db.get_file_record(clean_project, "dispatcher.py")
    assert file_record is not None
    assert file_record["chunk_count"] == 2  # route() + handle(), no preamble text here

    chunk_ids = db.get_chunk_ids_for_file(clean_project, "dispatcher.py")
    assert len(chunk_ids) == 2

    symbols = db.get_symbols_for_file(clean_project, "dispatcher.py")
    assert {s["symbol_name"] for s in symbols} == {"route", "handle"}


def test_reindexing_unchanged_file_is_a_no_op(monkeypatch, pg_conn, qdrant_client, clean_project, tmp_path):
    monkeypatch.setattr("embedder.embed_texts", _fake_embed_texts)
    monkeypatch.setattr("indexer.summarise_file", _fake_summarise_file)
    config_loader.set_thread_context(
        project={"name": clean_project, "indexer": {}}, resolved={}, project_root=str(tmp_path),
    )
    db.set_thread_db(pg_conn, qdrant_client)

    src = tmp_path / "a.py"
    src.write_text("def foo():\n    pass\n")
    assert indexer.index_file(src, project_root=str(tmp_path)) is True

    # Re-index the exact same content — content_sha256 is unchanged, so this
    # must be a no-op (return False), not a re-embed/re-write.
    assert indexer.index_file(src, project_root=str(tmp_path)) is False


def test_delta_upsert_only_touches_changed_chunks_on_edit(monkeypatch, pg_conn, qdrant_client, clean_project, tmp_path):
    monkeypatch.setattr("embedder.embed_texts", _fake_embed_texts)
    monkeypatch.setattr("indexer.summarise_file", _fake_summarise_file)
    config_loader.set_thread_context(
        project={"name": clean_project, "indexer": {}}, resolved={}, project_root=str(tmp_path),
    )
    db.set_thread_db(pg_conn, qdrant_client)

    src = tmp_path / "dispatcher.py"
    src.write_text(
        "def route(x):\n"
        "    return handle(x)\n\n"
        "def handle(x):\n"
        "    return x\n"
    )
    indexer.index_file(src, project_root=str(tmp_path))
    ids_before = db.get_chunk_ids_for_file(clean_project, "dispatcher.py")
    route_id_before = next(iter(db.get_chunk_ids_for_file(clean_project, "dispatcher.py")))

    # Change only handle()'s body — route() is byte-identical, so its stable
    # content-hash chunk ID must NOT change (delta-upsert, not full re-embed).
    src.write_text(
        "def route(x):\n"
        "    return handle(x)\n\n"
        "def handle(x):\n"
        "    return x * 2\n"
    )
    indexer.index_file(src, project_root=str(tmp_path))
    ids_after = db.get_chunk_ids_for_file(clean_project, "dispatcher.py")

    assert len(ids_after) == 2  # still 2 chunks (route + changed handle)
    assert len(ids_before & ids_after) == 1  # exactly one chunk ID survived unchanged (route)


def test_remove_file_deletes_chunks_and_all_related_rows(monkeypatch, pg_conn, qdrant_client, clean_project, tmp_path):
    monkeypatch.setattr("embedder.embed_texts", _fake_embed_texts)
    monkeypatch.setattr("indexer.summarise_file", _fake_summarise_file)
    config_loader.set_thread_context(
        project={"name": clean_project, "indexer": {}}, resolved={}, project_root=str(tmp_path),
    )
    db.set_thread_db(pg_conn, qdrant_client)

    src = tmp_path / "a.py"
    src.write_text("def foo():\n    pass\n")
    indexer.index_file(src, project_root=str(tmp_path))
    assert db.get_file_record(clean_project, "a.py") is not None
    assert len(db.get_chunk_ids_for_file(clean_project, "a.py")) == 1

    indexer.remove_file(src, project_root=str(tmp_path))

    assert db.get_file_record(clean_project, "a.py") is None
    assert db.get_chunk_ids_for_file(clean_project, "a.py") == set()
    assert db.get_symbols_for_file(clean_project, "a.py") == []
