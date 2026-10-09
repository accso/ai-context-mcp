"""
config_loader.py
Reads project config from /app/.resolved.json.

This module supports both the legacy single-project mode (one container, one
PROJECT_KEY env var) and the consolidated multi-project mode (one container,
multiple ProjectWatcher threads). In multi-project mode each thread MUST call
set_thread_context(project, resolved, project_root, extra_gitignore_prefix)
before using any accessor so that every per-thread DB/client instance and every
per-project value is scoped correctly.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

# Legacy module-level defaults (single-project container).
_RESOLVED_PATH = os.environ.get("RESOLVED_PATH", "/app/.resolved.json")
_PROJECT_KEY   = os.environ.get("PROJECT_KEY", "")

# Thread-local state — each ProjectWatcher runs in its own thread and therefore
# gets its own project scope without module-level contention.
_local = threading.local()


def set_thread_context(
    project: dict,
    resolved: dict,
    project_root: str,
    extra_gitignore_prefix: str = "/extra-gitignores",
) -> None:
    """Bind this thread to one project. Called by ProjectWatcher.run()."""
    _local.project = project
    _local.project_key = project["name"]
    _local.resolved = resolved
    _local.project_root = project_root
    _local.extra_gitignore_prefix = extra_gitignore_prefix


def _project_key() -> str:
    if hasattr(_local, "project_key"):
        return _local.project_key
    if _PROJECT_KEY:
        return _PROJECT_KEY
    raise RuntimeError(
        "PROJECT_KEY env var is not set and no thread context exists. "
        "Set PROJECT_KEY=<project-name> or call set_thread_context()."
    )


def _load() -> dict:
    if hasattr(_local, "project"):
        return _local.project

    global _project, _resolved
    try:
        return _project  # type: ignore[has-type]
    except NameError:
        pass

    rp = Path(_RESOLVED_PATH)
    if not rp.exists():
        raise RuntimeError(
            f"Resolved config not found at {rp}. "
            "Ensure .ai-context/.resolved.json is mounted into the container."
        )
    with open(rp) as f:
        _resolved = json.load(f)

    if not _PROJECT_KEY:
        raise RuntimeError(
            "PROJECT_KEY env var is not set. "
            "Each container must be started with PROJECT_KEY=<project-name>."
        )
    projects = _resolved.get("projects", {})
    if _PROJECT_KEY not in projects:
        raise RuntimeError(
            f"PROJECT_KEY='{_PROJECT_KEY}' not found in .resolved.json. "
            f"Available projects: {list(projects.keys())}"
        )
    _project = projects[_PROJECT_KEY]
    return _project


def _resolved() -> dict:
    if hasattr(_local, "resolved"):
        return _local.resolved

    _load()  # populates module-level _resolved for legacy mode
    try:
        return _resolved  # type: ignore[has-type]
    except NameError:
        raise RuntimeError("Resolved config not loaded.")


# ── Public accessors ──────────────────────────────────────────────────────────

def get_project() -> dict:            return _load()


def project_key() -> str:             return _project_key()


def project_name() -> str:            return _project_key()   # backward-compat alias


def repo_root() -> str:
    return _resolved()["repo_root"]


def project_root() -> str:
    """Absolute path to the project root *inside the container* for this thread."""
    if hasattr(_local, "project_root"):
        return _local.project_root
    return os.environ.get("PROJECT_ROOT", "/workspace")


def qdrant_url() -> str:
    return os.environ.get("QDRANT_URL", "http://localhost:6333")


def postgres_url() -> str:
    return os.environ.get(
        "POSTGRES_URL",
        "postgresql://ai_context:ai_context_local@localhost:5433/ai_context",
    )


_FALLBACK_PROFILE = {
    "components": [{"language": "unknown", "layer": "mixed", "framework": None, "build_system": None}],
    "monorepo": False,
}


def profile() -> dict:
    """Structured profile: {"components": [{"language", "layer", "framework",
    "build_system"}, ...], "monorepo": bool} — see "Profile auto-detection"
    in docs/configuration.md. Auto-detected by
    validate_config.py, never hand-authored in config.yaml.
    """
    return _load().get("profile", _FALLBACK_PROFILE)


def debounce_seconds() -> float:
    return float(_load()["indexer"].get("debounce_seconds", 2))


def reconcile_interval_minutes() -> int:
    """Interval in minutes between periodic full delta scans. 0 = disabled."""
    return int(_load()["indexer"].get("reconcile_interval_minutes", 30))


def polling_fallback_seconds() -> float:
    """
    Interval (seconds) for the fallback PollingObserver — see config.yaml's
    comment on this setting for why it exists (native inotify/FSEvents
    forwarding is not reliable on every container runtime/bind-mount
    configuration). 0 = disabled (native watcher only).
    """
    return float(_load()["indexer"].get("polling_fallback_seconds", 10))


def extra_skip_paths() -> list:
    return _load()["indexer"].get("extra_skip_paths") or []


def extra_gitignore_container_paths() -> list:
    """
    In-container paths for indexer.extra_gitignore_paths entries.

    In the consolidated watcher each project has its own namespace under
    /extra-gitignores to avoid collisions when multiple projects share one
    container. The compose file mounts each project's extra gitignore files at
    /extra-gitignores/{project_key}/{i}.gitignore in the same order they appear
    in the resolved config.
    """
    prefix = getattr(_local, "extra_gitignore_prefix", "/extra-gitignores")
    key = _project_key()
    n = len(_load()["indexer"].get("extra_gitignore_paths") or [])
    return [f"{prefix}/{key}/{i}.gitignore" for i in range(n)]


def max_chunk_chars() -> int:
    """Maximum characters per chunk before the indexer splits it."""
    return int(_load()["indexer"].get("max_chunk_chars", 6000))


def include_extensions() -> list:
    return _load()["indexer"].get("include_extensions", [
        ".kt", ".kts", ".ts", ".tsx", ".js", ".jsx",
        ".html", ".css", ".scss", ".json", ".yaml", ".yml",
        ".toml", ".sql", ".md", ".py", ".sh", "Dockerfile", ".swift",
    ])


def inference_url() -> str:
    """
    Base URL of the shared, mandatory inference service (embeddings +
    re-ranking) — see .ai-context/inference/. Not read from config.yaml:
    this is a fixed internal service on the Docker network, wired in via
    the INFERENCE_URL env var by scripts/start.sh — same for every
    project, never user-configurable (see docs/configuration.md).
    """
    return os.environ.get("INFERENCE_URL", "http://ai_context_inference:8080")


def llm_base_url() -> str:
    llm      = _load()["llm"]
    provider = llm.get("provider", "lmstudio")
    if provider == "lmstudio":
        return llm.get("lmstudio_base_url", "http://host.docker.internal:1234/v1")
    if provider == "omlx":
        return llm.get("omlx_base_url", "http://host.docker.internal:8000/v1")
    # ollama
    return os.environ.get("OLLAMA_URL", "http://ai_context_ollama:11434") + "/v1"


def llm_model() -> str:
    llm      = _load()["llm"]
    provider = llm.get("provider", "lmstudio")
    if provider == "ollama":
        return llm.get("ollama_model", "qwen2.5-coder:7b")
    return llm.get("model", "qwen2.5-coder-7b-instruct")


def llm_config() -> dict:
    return _load()["llm"]


def llm_disable_thinking() -> bool:
    return bool(_load()["llm"].get("disable_thinking", False))


def omlx_api_key() -> str | None:
    """
    Optional API key for omlx — only needed if your local omlx instance has
    authentication enabled.  Read from OMLX_API_KEY env var.
    """
    return os.environ.get("OMLX_API_KEY") or None


def api_headers(provider: str) -> dict:
    """Return HTTP headers required by the given provider (empty for local providers)."""
    if provider == "omlx":
        key = omlx_api_key()
        if key:
            return {"Authorization": f"Bearer {key}"}
    return {}


def llm_headers() -> dict:
    return api_headers(_load()["llm"].get("provider", "lmstudio"))
