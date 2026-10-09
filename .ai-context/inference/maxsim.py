"""
maxsim.py — ColBERT late-interaction (MaxSim) scoring.

FastEmbed's LateInteractionTextEmbedding returns per-token embeddings
(one row per token) for a query or document, but — unlike PyLate's
rank.rerank() — provides no built-in scoring/reranking utility. This module
is that missing piece: for every query token, take the highest similarity
to any single document token, then sum those per-token maxima across all
query tokens. This is what lets an exact identifier like
"PlatformUserRepository" or an annotation like "@KoinComponent" in the
query match that same token in a chunk directly, instead of being averaged
away inside one dense vector the way a bi-encoder would.
"""
import numpy as np


def maxsim_score(query_embedding: np.ndarray, doc_embedding: np.ndarray) -> float:
    """
    Computes the ColBERT MaxSim score between one query and one document.

    query_embedding: (n_query_tokens, dim)
    doc_embedding:    (n_doc_tokens, dim)

    Returns the sum, over every query token, of that token's highest
    similarity to any document token. Assumes both are already the
    similarity metric ColBERT models are trained for (dot product over
    L2-normalized token embeddings, as FastEmbed's ColBERT models emit).
    """
    if query_embedding.size == 0 or doc_embedding.size == 0:
        return 0.0
    sims = query_embedding @ doc_embedding.T  # (n_query_tokens, n_doc_tokens)
    return float(sims.max(axis=1).sum())


def rerank_by_maxsim(
    query_embedding: np.ndarray,
    doc_embeddings: dict[str, np.ndarray],
    top_n: int = 20,
) -> list[dict]:
    """
    Scores every candidate document against one query via MaxSim and
    returns the top_n, best-first.

    doc_embeddings: candidate id -> that candidate's per-token embeddings.
    Returns: [{"id": ..., "score": ...}, ...] sorted by score descending.
    """
    scored = [
        {"id": doc_id, "score": maxsim_score(query_embedding, doc_emb)}
        for doc_id, doc_emb in doc_embeddings.items()
    ]
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top_n]
