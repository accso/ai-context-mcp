"""
Integration tests for lsp-bridge/main.py's OWN routing/pooling/validation/
response-shaping logic — NOT a real end-to-end language-server round trip.

Real language-server integration was attempted first (multilspy +
jedi-language-server, see git history/session notes) and found to hang
reliably at the LSP `initialize` handshake in this sandboxed test
environment (confirmed via a 25s asyncio.wait_for timeout — the subprocess
never completes its handshake), a genuine environment/subprocess-
communication issue, not a hypothetical concern or impatience. Since a real
multilspy round trip couldn't be made to work reliably here, this suite
instead mocks LanguageServerPool.get_or_start (the one seam between "this
codebase's own code" and "third-party multilspy/language-server
behavior") and tests everything THIS codebase owns for real: request
validation, the StillStarting -> 503/Retry-After conversion, and
_to_resolved_location's 0-based-to-1-based line conversion + internal-vs-
external path classification (both pure functions, tested directly with
no mocking at all).

If real language-server round-trip testing is wanted later, it needs a
different environment than this one (e.g. inside the real lsp-bridge
Docker image, where docs/lsp-integration.md's "live-
validated" claims were presumably confirmed) or further debugging of the
hang itself — out of scope for this test-writing pass.
"""
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "sample-repo"
PROJECT_KEY = "test_lsp_project"


@pytest.fixture()
def main_module(tmp_path, monkeypatch):
    # RESOLVED_CONFIG_PATH / WORKSPACE_ROOT are read as module-level
    # constants at import time — set both, then force a fresh import so
    # main.py picks them up (a prior test's import would otherwise be
    # cached in sys.modules with stale values).
    workspace_root = tmp_path / "workspace"
    project_root = workspace_root / PROJECT_KEY
    project_root.mkdir(parents=True)
    # Symlink the shared fixture corpus's python subtree in, so file_path
    # values in requests below resolve to real files on disk.
    (project_root / "src").symlink_to(FIXTURES / "src")

    resolved = {
        "projects": {
            PROJECT_KEY: {
                "name": PROJECT_KEY,
                "lsp": {"enabled_languages": ["python"], "idle_timeout_minutes": 15},
            }
        }
    }
    resolved_path = tmp_path / ".resolved.json"
    resolved_path.write_text(json.dumps(resolved))

    monkeypatch.setenv("RESOLVED_CONFIG_PATH", str(resolved_path))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))

    sys.modules.pop("main", None)
    import main as lsp_main
    return lsp_main


@pytest.fixture()
def client(main_module):
    with TestClient(main_module.app) as c:
        yield c


class _FakeLocation(dict):
    """Matches the shape multilspy's request_definition/_references return —
    a plain dict with 'range' + 'relativePath' (see main.py's own
    _to_resolved_location, which reads exactly these keys)."""


def test_health_reports_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_resolve_rejects_invalid_kind(client):
    resp = client.post("/resolve", json={
        "project_key": PROJECT_KEY, "language": "python",
        "file_path": "src/python/main.py", "line": 1, "character": 0,
        "kind": "not-a-real-kind",
    })
    assert resp.status_code == 400


def test_resolve_rejects_language_not_enabled_for_project(client):
    resp = client.post("/resolve", json={
        "project_key": PROJECT_KEY, "language": "rust",  # only "python" is enabled
        "file_path": "src/python/main.py", "line": 1, "character": 0,
        "kind": "definition",
    })
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


def test_resolve_rejects_unknown_project_key(client):
    resp = client.post("/resolve", json={
        "project_key": "totally-unregistered-project", "language": "python",
        "file_path": "src/python/main.py", "line": 1, "character": 0,
        "kind": "definition",
    })
    assert resp.status_code == 404


def test_resolve_definition_success_converts_lines_to_1_based(client, main_module, monkeypatch):
    async def fake_get_or_start(project_key, language):
        class FakeServer:
            repository_root_path = str(Path(os.environ["WORKSPACE_ROOT"]) / PROJECT_KEY)

            async def request_definition(self, file_path, line, character):
                # multilspy Location: 0-based line 9 -> our API's 1-based line 10.
                return [_FakeLocation(
                    relativePath="src/python/dispatcher.py",
                    range={"start": {"line": 9, "character": 4}, "end": {"line": 9, "character": 9}},
                )]
        return FakeServer()

    monkeypatch.setattr(main_module.pool, "get_or_start", fake_get_or_start)

    resp = client.post("/resolve", json={
        "project_key": PROJECT_KEY, "language": "python",
        "file_path": "src/python/main.py", "line": 6, "character": 22,
        "kind": "definition",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["file_path"] == "src/python/dispatcher.py"
    assert body["results"][0]["start_line"] == 10  # 0-based 9 -> 1-based 10
    assert body["results"][0]["source_snippet"] is None  # in-project location, no snippet needed


def test_resolve_hover_success(client, main_module, monkeypatch):
    async def fake_get_or_start(project_key, language):
        class FakeServer:
            async def request_hover(self, file_path, line, character):
                return {"contents": {"value": "def route(event_type: str, payload: dict) -> str"}}
        return FakeServer()

    monkeypatch.setattr(main_module.pool, "get_or_start", fake_get_or_start)

    resp = client.post("/resolve", json={
        "project_key": PROJECT_KEY, "language": "python",
        "file_path": "src/python/dispatcher.py", "line": 10, "character": 4,
        "kind": "hover",
    })
    assert resp.status_code == 200
    assert "def route" in resp.json()["hover"]


def test_resolve_still_starting_returns_503_with_retry_after(client, main_module, monkeypatch):
    async def fake_get_or_start(project_key, language):
        raise main_module.StillStarting(language, 12.0)

    monkeypatch.setattr(main_module.pool, "get_or_start", fake_get_or_start)

    resp = client.post("/resolve", json={
        "project_key": PROJECT_KEY, "language": "python",
        "file_path": "src/python/main.py", "line": 1, "character": 0,
        "kind": "definition",
    })
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "12"
    assert resp.json()["still_starting"] is True


# ── Pure functions: _to_resolved_location / _read_source_snippet ─────────────
# No mocking needed at all — direct unit-level coverage of the 0-based ->
# 1-based conversion and internal-vs-external path classification this
# whole endpoint depends on.

def test_to_resolved_location_internal_path_has_no_snippet(main_module):
    repo_root = "/workspace/proj"
    loc = {"relativePath": "src/foo.py", "range": {"start": {"line": 4, "character": 2}, "end": {"line": 4, "character": 10}}}
    result = main_module._to_resolved_location(loc, repo_root)
    assert result.start_line == 5  # 0-based 4 -> 1-based 5
    assert result.source_snippet is None


def test_to_resolved_location_external_path_reads_snippet(main_module, tmp_path):
    repo_root = str(tmp_path / "proj")
    os.makedirs(repo_root)
    external_dir = tmp_path / "dep-cache" / "somepkg"
    external_dir.mkdir(parents=True)
    external_file = external_dir / "mod.py"
    external_file.write_text("\n".join(f"line{i}" for i in range(1, 40)))

    loc = {
        "relativePath": os.path.relpath(str(external_file), repo_root),
        "range": {"start": {"line": 19, "character": 0}, "end": {"line": 19, "character": 5}},
    }
    result = main_module._to_resolved_location(loc, repo_root)
    assert result.source_snippet is not None
    assert "line20" in result.source_snippet  # 0-based line 19 -> content "line20"
