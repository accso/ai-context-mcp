"""
llm_summariser.py
Generates L1 file summaries using the local LLM (via LM Studio or Ollama).
The summary captures: purpose, public API surface, key types/classes/functions,
and architectural role (layer, DI registrations, routing).
Kept under 250 tokens for efficient context injection.

Thinking-model compatibility:
  Any model that emits <think>…</think> blocks (Qwen3-thinking, DeepSeek-R1, etc.)
  is handled automatically — the think block is stripped before the summary is stored.
  Set llm.disable_thinking: true in config.yaml to also suppress the thinking phase
  at the API level (adds /no_think to the system prompt and sets thinking_budget: 0),
  which saves tokens and latency on Qwen3* thinking variants.
"""
import re
import httpx
import config_loader as cfg

_SYSTEM_PROMPT = """You are a senior software engineer writing concise, technically precise documentation for an AI coding assistant.

Given a source file, write a summary (4–6 sentences) covering ALL of the following that apply:
1. Purpose and architectural role (e.g. domain entity, use-case, repository, REST route handler, Koin module, React component)
2. Public classes, interfaces, functions, or components it exposes — include their exact names
3. Key method signatures or endpoint paths where relevant (e.g. "GET /api/v1/users", "fun listUsers(page, size, tenantId)")
4. Important dependencies, injected services, or integrations
5. Any design patterns in use (e.g. onion architecture layer, CQRS, repository pattern)

Rules: factual and technical only, NO code blocks, NO filler phrases, max 250 tokens."""

_USER_TEMPLATE = """File: {path}
Language: {language}

{content}

---
Write the summary now:"""

# Matches <think>…</think> blocks emitted by reasoning models (Qwen3-thinking, DeepSeek-R1).
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """Remove <think>…</think> chain-of-thought blocks from model output."""
    return _THINK_RE.sub("", text).strip()


def summarise_file(rel_path: str, content: str, language: str) -> str:
    """Call the local LLM to produce an L1 summary for a file."""
    # Truncate very large files to first 8000 chars to keep LLM input manageable
    truncated = content[:8000] + ("\n... [truncated]" if len(content) > 8000 else "")

    disable_thinking = cfg.llm_disable_thinking()
    system_prompt = _SYSTEM_PROMPT
    if disable_thinking:
        # /no_think suffix suppresses chain-of-thought on Qwen3* thinking variants
        system_prompt = system_prompt + "\n/no_think"

    payload: dict = {
        "model": cfg.llm_model(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": _USER_TEMPLATE.format(
                path=rel_path,
                language=language,
                content=truncated,
            )},
        ],
        "max_tokens":  300,
        "temperature": 0.1,
        "stream":      False,
    }
    if disable_thinking:
        # LM Studio / llama.cpp supports thinking_budget: 0 for compatible models
        payload["thinking_budget"] = 0

    resp = httpx.post(
        cfg.llm_base_url().rstrip("/") + "/chat/completions",
        json=payload,
        headers=cfg.llm_headers(),
        timeout=60.0,
    )
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    # Some APIs (reasoning / thinking models) omit "content" or set it to null,
    # returning the actual text in "reasoning_content" instead.
    raw = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    if not raw:
        raise ValueError(f"LLM returned empty content. Response keys: {list(msg.keys())}")
    return _strip_thinking(raw)


class LLMClient:
    """Thin wrapper used by doc_tree_builder to call the configured local LLM.

    Pass *model_override* to use a different model than the default shared.llm.model
    — this is what honours the ``doc_index.tree_llm_model`` config field.
    When *model_override* is None (default) the shared LLM model is used.
    """

    def __init__(self, model_override: str | None = None) -> None:
        self._model = model_override or cfg.llm_model()

    def chat_completion(
        self,
        messages: list[dict],
        max_tokens: int = 300,
        temperature: float = 0.1,
    ) -> str:
        disable_thinking = cfg.llm_disable_thinking()
        payload: dict = {
            "model":       self._model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "stream":      False,
        }
        if disable_thinking:
            payload["thinking_budget"] = 0
        resp = httpx.post(
            cfg.llm_base_url().rstrip("/") + "/chat/completions",
            json=payload,
            headers=cfg.llm_headers(),
            timeout=90.0,
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        raw = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        if not raw:
            raise ValueError(f"LLM returned empty content. Response keys: {list(msg.keys())}")
        return _strip_thinking(raw)

