"""
embedder.py
Wraps the internal inference service's POST /embeddings endpoint (see
.ai-context/inference/main.py) — a mandatory, always-on internal service
(FastEmbed/onnxruntime, no torch), not LM Studio/omlx/ollama. The embedding
model is hardcoded there, not read from config.yaml: its vector dimension
is baked into every project's Qdrant collection schema at creation time,
so changing it is a data-migration decision, not a config toggle (see
docs/configuration.md).

The client is still cached per thread so each ProjectWatcher reuses one client
for its lifetime, while different projects never share a client object.
"""
import threading
import httpx
import config_loader as cfg

_local = threading.local()

# Legacy module-level fallback.
_client = None


def _get_client():
    global _client
    if hasattr(_local, "client"):
        client = _local.client
        if client is None:
            client = httpx.Client(
                base_url=cfg.inference_url(),
                timeout=60.0,
            )
            _local.client = client
        return client
    if _client is None:
        _client = httpx.Client(
            base_url=cfg.inference_url(),
            timeout=60.0,
        )
    return _client


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """
    Embed a list of strings. Returns list of float vectors.

    input_type: "document" (default — used when indexing chunks for
    storage) or "query" (used only for embedding a search query at
    retrieval time — mcp-server, not the watcher, does that side). This
    lets the inference service apply nomic-embed's required task prefix
    ("search_document: "/"search_query: ") correctly — a raw
    OpenAI-passthrough to LM Studio/omlx never applied any prefix at all.
    """
    if not texts:
        return []
    client = _get_client()
    results = []
    batch_size = 32
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = client.post("/embeddings", json={
            "input": batch,
            "input_type": input_type,
        })
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda x: x["index"])
        results.extend([d["embedding"] for d in data])
    return results
