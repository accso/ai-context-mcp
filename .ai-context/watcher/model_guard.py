"""
model_guard.py
Check that the configured LLM model is actually available at runtime. If
it's missing, emit a clear warning and return False so that callers can
skip destructive operations (initial indexing / re-indexing) and avoid
overwriting a restored vector database.

Embedding availability is NOT checked here (unlike before this module's
LM Studio/omlx-era design): the inference service (see
.ai-context/inference/) is a mandatory, always-on Docker Compose service
with its own container healthcheck — not an optional, user-managed
external process the way LM Studio/omlx is for the LLM. See
docs/architecture-deep-dive.md.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _api_headers(provider: str, api_key: str | None) -> dict[str, str]:
    if provider == "omlx" and api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def _localhost_url(base_url: str) -> str:
    """
    When running the watcher container on the host network, or when the check
    script is executed on the host, host.docker.internal may not be resolvable.
    Allow a transparent fallback to localhost so the operator gets a useful
    pre-flight check.
    """
    return base_url.replace("host.docker.internal", "localhost")


def _fetch_models(base_url: str, headers: dict[str, str], timeout: float = 10.0) -> list[str] | None:
    """
    Query a provider's /v1/models endpoint. Returns a list of model IDs, or
    None if the endpoint is unreachable / not supported.
    """
    urls_to_try = [_localhost_url(base_url), base_url]
    seen = set()
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
        except Exception:
            continue
    return None


def _model_available(
    base_url: str,
    model_name: str,
    provider: str,
    api_key: str | None,
) -> bool:
    if not model_name:
        return False

    available = _fetch_models(base_url, _api_headers(provider, api_key))
    if available is None:
        # Provider doesn't expose /models (e.g. some Ollama builds). Fall back
        # to lightweight embeddings/ping; call a non-existent model and rely on
        # the error text to decide whether the model itself is missing.
        return _probe_model_via_embedding(base_url, model_name, provider, api_key)

    return model_name in available


def _probe_model_via_embedding(base_url: str, model_name: str, provider: str, api_key: str | None) -> bool:
    """
    Lightweight fallback check for providers without /v1/models. Send a tiny
    embedding request and interpret the response.
    """
    urls_to_try = [_localhost_url(base_url), base_url]
    seen = set()
    for url in urls_to_try:
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = httpx.post(
                f"{url.rstrip('/')}/embeddings",
                json={"model": model_name, "input": ["test"]},
                headers=_api_headers(provider, api_key),
                timeout=15.0,
            )
            if resp.status_code < 400:
                return True
            text = resp.text.lower()
            if any(phrase in text for phrase in ("model not found", "could not find model", "unknown model")):
                return False
            # Provider reachable but model unrecognized → treat as missing to be safe
            return False
        except Exception:
            continue
    return False


def _config_key(config: dict[str, Any], key: str) -> Any:
    return config.get(key)


def assert_models_available(config: dict[str, Any], api_key: str | None = None) -> bool:
    """
    Check the configured LLM model is available.

    Returns True only if the LLM is reachable. When it's missing, logs a
    user-actionable warning with the exact model name and the provider URL
    to check. `config` only needs an "llm" key (an "embedding" key, if
    present, is ignored — see module docstring).
    """
    ok = True

    llm = config.get("llm") or {}
    llm_provider = llm.get("provider", "lmstudio")
    llm_model_name = llm.get("ollama_model") if llm_provider == "ollama" else llm.get("model", "")
    llm_url = (
        llm.get("lmstudio_base_url", "http://host.docker.internal:1234/v1") if llm_provider == "lmstudio"
        else llm.get("omlx_base_url", "http://host.docker.internal:8000/v1") if llm_provider == "omlx"
        else (config.get("ollama_url") or "http://ai_context_ollama:11434") + "/v1"
    )

    if not _model_available(llm_url, llm_model_name, llm_provider, api_key):
        log.warning(
            f"[model-check] Configured LLM model is not available: '{llm_model_name}' "
            f"at {llm_url} (provider: {llm_provider}). "
            f"Please load the model and ensure the server is running. "
            f"Skipping destructive index operations to protect the existing vector database."
        )
        ok = False

    return ok
