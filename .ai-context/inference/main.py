"""
main.py — Inference Service (embeddings + re-ranking)

Both retrieval-support responsibilities merged into one FastAPI app,
backed entirely by FastEmbed (onnxruntime) — no torch, no external LM
Studio/omlx/ollama dependency for either. Both models are hardcoded here
(not user-configurable via config.yaml): the embedding model's vector
dimension is baked into every project's Qdrant collection schema at
creation time, so changing it isn't a config toggle, it's a data-migration
decision (see `mise run ai-context-reset`). Re-ranking has no such
constraint (stateless, query-time only), but the model/backend choice is
still fixed here rather than exposed in YAML — see docs/configuration.md.

Both endpoints below use plain "def" (not "async def") handlers, so
FastAPI/Starlette automatically offloads each request onto its own thread
pool worker — one long /embeddings batch (e.g. during a bulk reindex) does
not block a concurrent /rerank call's event-loop turn. Real CPU-core
contention between the two under sustained parallel load is a separate,
lower-priority concern than event-loop blocking; not addressed here unless
it's ever actually measured as a problem (see architecture-deep-dive.md).

── Embeddings ──────────────────────────────────────────────────────────────

  POST /embeddings  { "input": [str, ...], "input_type": "document" | "query" }
    -> { "data": [{"embedding": [float, ...], "index": int}, ...] }

  Backed by fastembed.TextEmbedding("nomic-ai/nomic-embed-text-v1.5-Q").
  Uses query_embed()/passage_embed() (not the bare embed()) so FastEmbed
  applies nomic-embed's required task prefix ("search_query: " /
  "search_document: ") correctly per input_type — the raw OpenAI-passthrough
  this replaces (LM Studio/omlx) never applied any prefix at all.

── Re-ranking ───────────────────────────────────────────────────────────────

  POST /rerank  { "query": str, "candidates": [{id, text, score}], "top_n": int }
    -> [{"id", "score", "original_score"}, ...]

  Backend selectable only via the RERANKER_BACKEND Docker build ARG (never
  config.yaml — see module docstring above):

  RERANKER_BACKEND=colbert  (default)
    FastEmbed LateInteractionTextEmbedding + maxsim.rerank_by_maxsim (see
    maxsim.py) — token-level MaxSim re-ranking. Keeps per-token embeddings
    for both query and document, directly matching tokens like
    "PlatformUserRepository" or "@KoinComponent" without losing them in a
    single dense vector, the way a bi-encoder would.

    Model: "answerdotai/answerai-colbert-small-v1"  (~130 MB, code-tuned)

  RERANKER_BACKEND=cross-encoder  (lightweight fallback)
    FastEmbed TextCrossEncoder.rerank() — simpler, faster, smaller. Use
    when inference latency is the top constraint over quality.

    Model: "Xenova/ms-marco-MiniLM-L-6-v2"  (ONNX conversion of the same
    cross-encoder/ms-marco-MiniLM-L-6-v2 weights used before this migration)

  Both backends now ride the same fastembed/onnxruntime dependency (unlike
  the previous pylate-vs-sentence-transformers split), so keeping both
  available costs little — see architecture-deep-dive.md "Re-ranking".
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from maxsim import rerank_by_maxsim

log = logging.getLogger("inference")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Embedding model — hardcoded, see module docstring ────────────────────────
EMBEDDING_MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5-Q"
EMBEDDING_DIMENSION = 768

_embedding_model = None


def _load_embedding_model() -> None:
    global _embedding_model
    from fastembed import TextEmbedding  # noqa: PLC0415
    log.info(f"[embedding] Loading: {EMBEDDING_MODEL_NAME}")
    _embedding_model = TextEmbedding(EMBEDDING_MODEL_NAME)
    log.info("[embedding] Ready.")


# ── Re-ranker model — backend fixed via build-time env var, see docstring ───
BACKEND = os.getenv("RERANKER_BACKEND", "colbert").lower()

_DEFAULT_MODELS = {
    "colbert":       "answerdotai/answerai-colbert-small-v1",
    "cross-encoder": "Xenova/ms-marco-MiniLM-L-6-v2",
}
RERANKER_MODEL_NAME = os.getenv("RERANKER_MODEL", _DEFAULT_MODELS.get(BACKEND, ""))

_colbert_model = None
_cross_encoder = None


def _load_colbert() -> None:
    global _colbert_model
    from fastembed import LateInteractionTextEmbedding  # noqa: PLC0415
    log.info(f"[colbert] Loading: {RERANKER_MODEL_NAME}")
    _colbert_model = LateInteractionTextEmbedding(RERANKER_MODEL_NAME)
    log.info("[colbert] Ready.")


def _load_cross_encoder() -> None:
    global _cross_encoder
    from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: PLC0415
    log.info(f"[cross-encoder] Loading: {RERANKER_MODEL_NAME}")
    _cross_encoder = TextCrossEncoder(RERANKER_MODEL_NAME)
    log.info("[cross-encoder] Ready.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _load_embedding_model()
    if BACKEND == "colbert":
        _load_colbert()
    else:
        _load_cross_encoder()
    yield


app = FastAPI(lifespan=lifespan)


# ── Pydantic schemas — embeddings ────────────────────────────────────────────

class EmbeddingsRequest(BaseModel):
    input: list[str]
    input_type: Literal["document", "query"] = "document"


class EmbeddingItem(BaseModel):
    embedding: list[float]
    index: int


class EmbeddingsResponse(BaseModel):
    data: list[EmbeddingItem]


# ── Pydantic schemas — re-ranking (unchanged wire shape from the previous
#    standalone reranker service) ─────────────────────────────────────────────

class Candidate(BaseModel):
    id:    str
    text:  str
    score: float   # original Qdrant semantic score


class RerankRequest(BaseModel):
    query:      str
    candidates: list[Candidate]
    top_n:      int = 20


class RankedResult(BaseModel):
    id:             str
    score:          float   # re-ranker score (higher = more relevant)
    original_score: float   # original Qdrant score kept for debugging


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "embedding_model": EMBEDDING_MODEL_NAME,
        "reranker_backend": BACKEND,
        "reranker_model": RERANKER_MODEL_NAME,
    }


@app.post("/embeddings", response_model=EmbeddingsResponse)
def embeddings(req: EmbeddingsRequest) -> EmbeddingsResponse:
    if not req.input:
        return EmbeddingsResponse(data=[])
    if req.input_type == "query":
        vectors = _embedding_model.query_embed(req.input)
    else:
        vectors = _embedding_model.passage_embed(req.input)
    return EmbeddingsResponse(data=[
        EmbeddingItem(embedding=vec.tolist(), index=i)
        for i, vec in enumerate(vectors)
    ])


@app.post("/rerank", response_model=list[RankedResult])
def rerank(req: RerankRequest) -> list[RankedResult]:
    if not req.candidates:
        return []
    if BACKEND == "colbert":
        return _rerank_colbert(req)
    return _rerank_cross_encoder(req)


def _rerank_colbert(req: RerankRequest) -> list[RankedResult]:
    """
    FastEmbed late-interaction (ColBERT/MaxSim) re-ranking, index-free —
    see maxsim.py for the scoring itself. FastEmbed's
    LateInteractionTextEmbedding has no built-in rerank()/scoring utility
    (unlike the PyLate library this replaces), so maxsim.rerank_by_maxsim
    provides that missing piece.
    """
    ids  = [c.id for c in req.candidates]
    docs = [c.text for c in req.candidates]
    original_scores = {c.id: c.score for c in req.candidates}

    try:
        query_embedding = next(iter(_colbert_model.query_embed([req.query])))
        doc_embeddings = list(_colbert_model.passage_embed(docs))
        doc_embeddings_by_id = dict(zip(ids, doc_embeddings))
        ranked = rerank_by_maxsim(query_embedding, doc_embeddings_by_id, top_n=req.top_n)
    except Exception as exc:
        log.error(f"ColBERT rerank error: {exc} — falling back to original order")
        return [
            RankedResult(id=c.id, score=c.score, original_score=c.score)
            for c in req.candidates
        ][: req.top_n]

    return [
        RankedResult(
            id=r["id"],
            score=r["score"],
            original_score=original_scores[r["id"]],
        )
        for r in ranked
    ]


def _rerank_cross_encoder(req: RerankRequest) -> list[RankedResult]:
    """FastEmbed cross-encoder (lightweight fallback) — scores only, so this
    function still owns sorting + top_n slicing, same as the sentence-
    transformers-based implementation it replaces."""
    docs = [c.text for c in req.candidates]
    scores = list(_cross_encoder.rerank(req.query, docs))
    results = [
        RankedResult(id=c.id, score=float(s), original_score=c.score)
        for c, s in zip(req.candidates, scores)
    ]
    results.sort(key=lambda r: r.score, reverse=True)
    return results[: req.top_n]
