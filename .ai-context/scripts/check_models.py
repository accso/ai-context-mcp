#!/usr/bin/env python3
"""
check_models.py — Verify the configured LLM model is available.

Reads .resolved.json, queries the LLM provider's OpenAI-compatible
/v1/models endpoint, and exits non-zero if the configured model is
missing. This prevents a misconfigured LM Studio / omlx instance from
silently producing no L1 file summaries during the first init.

Embedding availability is NOT checked here: the inference service (see
.ai-context/inference/) is a mandatory, always-on Docker Compose service
with its own container healthcheck, not a user-managed external process
the way LM Studio/omlx is for the LLM (see docs/architecture-deep-dive.md).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


def _api_headers(provider: str, api_key: str | None) -> dict[str, str]:
    if provider == "omlx" and api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def _localhost_url(base_url: str) -> str:
    """
    When running check_models.py on the host, host.docker.internal is not
    resolvable on some machines (notably macOS without Docker Desktop). Replace
    it with localhost so the operator can verify their local LM Studio / omlx
    server before starting the Docker stack.
    """
    return base_url.replace("host.docker.internal", "localhost")


def _fetch_models(base_url: str, headers: dict[str, str], timeout: float = 10.0) -> list[str] | None:
    urls_to_try = [_localhost_url(base_url), base_url]
    seen = set()
    last_exc: Exception | None = None
    for url in urls_to_try:
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = httpx.get(f"{url.rstrip('/')}/models", headers=headers, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data") or data.get("models") or []
            return [str(m.get("id") or m.get("model")) for m in models if isinstance(m, dict)]
        except Exception as exc:
            last_exc = exc
            continue
    print(f"WARN  Cannot reach model provider at {base_url}: {last_exc}", file=sys.stderr)
    return None


def _probe_embedding(base_url: str, model_name: str, headers: dict[str, str]) -> bool:
    urls_to_try = [_localhost_url(base_url), base_url]
    seen = set()
    last_exc: Exception | None = None
    for url in urls_to_try:
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = httpx.post(
                f"{url.rstrip('/')}/embeddings",
                json={"model": model_name, "input": ["test"]},
                headers=headers,
                timeout=15.0,
            )
            if resp.status_code < 400:
                return True
            text = resp.text.lower()
            missing_phrases = ("model not found", "could not find model", "unknown model")
            if any(p in text for p in missing_phrases):
                print(f"ERR   Embedding model '{model_name}' was rejected by {base_url}. Response: {resp.text[:200]}")
                return False
            print(f"WARN  Embedding model '{model_name}' probe failed at {base_url}: {resp.text[:200]}")
            return False
        except Exception as exc:
            last_exc = exc
            continue
    print(f"ERR   Cannot probe embedding model '{model_name}' at {base_url}: {last_exc}")
    return False


def _check_model(label: str, base_url: str, model_name: str, provider: str, api_key: str | None) -> bool:
    if not model_name:
        print(f"ERR   {label} model name is not configured.")
        return False

    headers = _api_headers(provider, api_key)
    available = _fetch_models(base_url, headers)

    if available is not None:
        if model_name in available:
            print(f"OK    {label}: '{model_name}' is available at {base_url}")
            return True
        print(f"ERR   {label}: '{model_name}' is NOT available at {base_url}. Available models:")
        for mid in available:
            print(f"      - {mid}")
        return False

    print(f"WARN  {label}: {base_url} does not expose /v1/models — probing /v1/embeddings...")
    return _probe_embedding(base_url, model_name, headers)


def _ollama_url(resolved: dict) -> str:
    # Prefer explicit env override; from within Docker the fallback is the
    # shared compose service name.
    return resolved.get("ollama_url") or "http://ai_context_ollama:11434"


def _check_project(project: dict, ollama_base: str, api_key: str | None) -> bool:
    ok = True

    llm = project.get("llm") or {}
    llm_provider = llm.get("provider", "lmstudio")
    if llm_provider == "lmstudio":
        llm_url = llm.get("lmstudio_base_url", "http://host.docker.internal:1234/v1")
    elif llm_provider == "omlx":
        llm_url = llm.get("omlx_base_url", "http://host.docker.internal:8000/v1")
    else:
        llm_url = ollama_base + "/v1"
    llm_model = llm.get("ollama_model") if llm_provider == "ollama" else llm.get("model", "")

    if not _check_model("llm", llm_url, llm_model, llm_provider, api_key):
        ok = False

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Check configured models are available.")
    parser.add_argument("--resolved", required=True, help="Path to .resolved.json")
    args = parser.parse_args()

    with open(args.resolved) as f:
        resolved = json.load(f)

    api_key = resolved.get("ollama_api_key") or None
    ollama_base = _ollama_url(resolved)
    ok = True

    for project_key, project in resolved.get("projects", {}).items():
        print(f"--> Checking models for project: {project_key}")
        if not _check_project(project, ollama_base, api_key):
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
