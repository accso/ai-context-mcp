"""
index_fixture_repo.py — standalone E2E helper script (not a pytest test
itself). Indexes tests/fixtures/sample-repo into a real, already-running
disposable Postgres + Qdrant (see mcp-server/test/e2e/test-stack.ts, which
spawns this script as a subprocess), using the SAME real watcher code path
(indexer.run_full_index + l0_generator.run_l0) as production — only the
embedding/LLM HTTP calls are stubbed (deterministic, content-derived fake
vectors matching the scheme mcp-server/test/e2e's fake inference server
uses on the query side, so semantic_search has at least rough, consistent
signal to rank against — real embedding QUALITY is explicitly out of scope
here, same as the Tier 2 indexer integration tests).

Usage: PROJECT_KEY=... PROJECT_ROOT=... POSTGRES_URL=... QDRANT_URL=... \
       python3 index_fixture_repo.py
"""
import os
import sys

WATCHER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "watcher")
sys.path.insert(0, os.path.abspath(WATCHER_DIR))

import psycopg2
from qdrant_client import QdrantClient

import config_loader
import db
import embedder
import indexer
import l0_generator

FAKE_VECTOR_DIM = 8


def fake_embed(text: str, dim: int = FAKE_VECTOR_DIM) -> list[float]:
    # Deterministic, content-derived — MUST match the fake inference server's
    # formula in mcp-server/test/e2e/fake-inference-server.ts exactly, so
    # query-time and index-time vectors are comparable.
    total = sum(ord(c) for c in text)
    return [((total * (i + 1)) % 1000) / 1000.0 for i in range(dim)]


def fake_embed_texts(texts: list[str]) -> list[list[float]]:
    return [fake_embed(t) for t in texts]


def fake_summarise_file(rel_path: str, content: str, language: str) -> str:
    return f"Fake L1 summary of {rel_path} ({language})."


def main() -> None:
    project_key = os.environ["PROJECT_KEY"]
    project_root = os.environ["PROJECT_ROOT"]
    postgres_url = os.environ["POSTGRES_URL"]
    qdrant_url = os.environ["QDRANT_URL"]

    embedder.embed_texts = fake_embed_texts
    indexer.summarise_file = fake_summarise_file

    pg_conn = psycopg2.connect(postgres_url)
    pg_conn.autocommit = True
    qd_client = QdrantClient(url=qdrant_url)

    try:
        qd_client.delete_collection(project_key)
    except Exception:
        pass
    qd_client.create_collection(
        collection_name=project_key,
        vectors_config={"size": FAKE_VECTOR_DIM, "distance": "Cosine"},
    )

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM projects WHERE project_key = %s", (project_key,))
        cur.execute(
            "INSERT INTO projects (project_key, display_name, profile, root_path) "
            "VALUES (%s, %s, %s, %s)",
            (project_key, "E2E Fixture Project", "{}", ""),
        )

    config_loader.set_thread_context(
        project={
            "name": project_key,
            "indexer": {
                "include_extensions": [
                    ".kt", ".ts", ".tsx", ".js", ".jsx", ".vue", ".py",
                    ".java", ".cs", ".md", ".sql", ".yaml", ".yml",
                ],
            },
            "doc_index": {"enabled": True},
        },
        resolved={},
        project_root=project_root,
    )
    db.set_thread_db(pg_conn, qd_client)

    indexer.run_full_index()
    l0_generator.run_l0()

    print(f"[index_fixture_repo] Indexed '{project_key}' from {project_root} into {postgres_url} / {qdrant_url}")


if __name__ == "__main__":
    main()
