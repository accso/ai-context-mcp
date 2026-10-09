"""
conftest.py — disposable Postgres + Qdrant fixtures for watcher Tier 2
integration tests. Reuses the exact images infrastructure/docker-compose.yml
runs (postgres:16-alpine, qdrant/qdrant:v1.18.2) and the real schema
(infrastructure/init.sql), so these tests exercise the actual DB/vector-store
behavior indexer.py depends on, not an approximation.

Every container carries --label com.ai-context.test=true and --rm, on
distinct test-only ports (15433/16334 — not the real dev stack's 5433/6333,
and not the mcp-server Tier 2 suite's own 15432, so both can run
concurrently without colliding). See scripts/test-cleanup.sh, which targets
exactly this label as the safety net if a run crashes before teardown.
"""
import subprocess
import time
from pathlib import Path

import psycopg2
import pytest
from qdrant_client import QdrantClient

PG_PORT = 15433
QDRANT_PORT = 16334
TEST_LABEL = "com.ai-context.test=true"
INIT_SQL = str((Path(__file__).resolve().parents[3] / "infrastructure" / "init.sql"))


def _docker_run(*args: str) -> str:
    result = subprocess.run(["docker", "run", "-d", "--rm", "--label", TEST_LABEL, *args],
                             capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture(scope="session")
def pg_conn():
    name = f"ai_context_test_pg_watcher_{int(time.time())}"
    _docker_run(
        "--name", name,
        "-e", "POSTGRES_DB=ai_context_test", "-e", "POSTGRES_USER=ai_context",
        "-e", "POSTGRES_PASSWORD=test",
        "-p", f"{PG_PORT}:5432",
        "-v", f"{INIT_SQL}:/docker-entrypoint-initdb.d/init.sql:ro",
        "postgres:16-alpine",
    )
    conn = None
    deadline = time.time() + 30
    last_err = None
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(
                host="localhost", port=PG_PORT, dbname="ai_context_test",
                user="ai_context", password="test",
            )
            conn.autocommit = True
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if conn is None:
        raise RuntimeError(f"Postgres test container never became ready: {last_err}")

    yield conn

    conn.close()
    subprocess.run(["docker", "stop", name], capture_output=True)


@pytest.fixture(scope="session")
def qdrant_client():
    name = f"ai_context_test_qdrant_watcher_{int(time.time())}"
    _docker_run("--name", name, "-p", f"{QDRANT_PORT}:6333", "qdrant/qdrant:v1.18.2")

    client = None
    deadline = time.time() + 30
    last_err = None
    while time.time() < deadline:
        try:
            client = QdrantClient(url=f"http://localhost:{QDRANT_PORT}")
            client.get_collections()
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if client is None:
        raise RuntimeError(f"Qdrant test container never became ready: {last_err}")

    yield client

    subprocess.run(["docker", "stop", name], capture_output=True)


@pytest.fixture()
def clean_project(pg_conn, qdrant_client):
    """
    Truncates every project-scoped table and recreates a fresh Qdrant
    collection before each test — so tests don't leak state into each
    other even though the containers themselves are session-scoped (much
    faster than a fresh container per test).
    """
    project_key = "test_watcher_project"
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM projects WHERE project_key = %s", (project_key,))
        cur.execute(
            "INSERT INTO projects (project_key, display_name, profile, root_path) "
            "VALUES (%s, %s, %s, %s)",
            (project_key, "Test Watcher Project", "{}", ""),
        )
    try:
        qdrant_client.delete_collection(project_key)
    except Exception:
        pass
    qdrant_client.create_collection(
        collection_name=project_key,
        vectors_config={"size": 8, "distance": "Cosine"},
    )
    return project_key
