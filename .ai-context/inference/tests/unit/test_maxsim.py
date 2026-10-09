"""
Unit tests for inference/maxsim.py — pure ColBERT late-interaction MaxSim
scoring, no model loading, no Docker/network. Tier 1 per
docs/testing.md / ai-context-tdd.instructions.md: this is genuinely pure
numeric logic (unlike the rest of main.py's thin FastAPI routing, which
stays Tier 2), so it gets its own dedicated unit test.

MaxSim (ColBERT late-interaction scoring): for every query token embedding,
take the highest similarity to any single document token embedding, then
sum those per-token maxima across all query tokens. This is what lets
"PlatformUserRepository" in the query match that exact token in a chunk
without being averaged away by a single dense vector.
"""
import numpy as np
import pytest

from maxsim import maxsim_score, rerank_by_maxsim


def test_maxsim_score_sums_per_query_token_best_match():
    # 2 query tokens, dim=2; 3 document tokens.
    query = np.array([[1.0, 0.0], [0.0, 1.0]])
    doc = np.array([[1.0, 0.0], [0.0, 0.5], [0.9, 0.1]])
    # Query token 0 best matches doc token 0 (dot=1.0).
    # Query token 1 best matches doc token 1 (dot=0.5).
    assert maxsim_score(query, doc) == pytest.approx(1.5)


def test_maxsim_score_single_token_each_is_plain_dot_product():
    query = np.array([[0.6, 0.8]])
    doc = np.array([[0.6, 0.8]])
    assert maxsim_score(query, doc) == pytest.approx(1.0)


def test_maxsim_score_empty_document_returns_zero():
    query = np.array([[1.0, 0.0]])
    doc = np.empty((0, 2))
    assert maxsim_score(query, doc) == 0.0


def test_maxsim_score_empty_query_returns_zero():
    query = np.empty((0, 2))
    doc = np.array([[1.0, 0.0]])
    assert maxsim_score(query, doc) == 0.0


def test_rerank_by_maxsim_sorts_candidates_best_first():
    query = np.array([[1.0, 0.0]])
    # doc "a" only weakly aligned with the query; doc "b" aligned perfectly.
    docs = {
        "a": np.array([[0.1, 0.9]]),
        "b": np.array([[1.0, 0.0]]),
    }
    ranked = rerank_by_maxsim(query, docs, top_n=20)
    assert [r["id"] for r in ranked] == ["b", "a"]
    assert ranked[0]["score"] > ranked[1]["score"]


def test_rerank_by_maxsim_respects_top_n():
    query = np.array([[1.0, 0.0]])
    docs = {
        "a": np.array([[1.0, 0.0]]),
        "b": np.array([[0.9, 0.1]]),
        "c": np.array([[0.0, 1.0]]),
    }
    ranked = rerank_by_maxsim(query, docs, top_n=2)
    assert len(ranked) == 2
    assert [r["id"] for r in ranked] == ["a", "b"]
