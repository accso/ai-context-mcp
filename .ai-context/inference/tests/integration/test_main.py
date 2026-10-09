"""
Integration tests for inference/main.py — backend selection (env-var
driven), request/response schema handling, and endpoint routing for both
/embeddings and /rerank, via FastAPI's TestClient. Tier 2 per
docs/testing.md (same classification as the reranker service it replaces).

Model loading itself (_load_embedding_model/_load_colbert/
_load_cross_encoder) is monkeypatched to a no-op for every test here —
this suite is about the service's own routing/validation/backend-selection
logic, not about validating FastEmbed/onnxruntime themselves, and
deliberately avoids requiring any real ONNX model download to run.
"""
import importlib

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def main_module(monkeypatch):
    import main as inference_main
    monkeypatch.setattr(inference_main, "_load_embedding_model", lambda: None)
    monkeypatch.setattr(inference_main, "_load_colbert", lambda: None)
    monkeypatch.setattr(inference_main, "_load_cross_encoder", lambda: None)
    return inference_main


@pytest.fixture()
def client(main_module):
    with TestClient(main_module.app) as c:
        yield c


# ── /health ───────────────────────────────────────────────────────────────────

def test_health_reports_embedding_and_reranker_info(client, main_module):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["embedding_model"] == main_module.EMBEDDING_MODEL_NAME
    assert body["reranker_backend"] == main_module.BACKEND
    assert body["reranker_model"] == main_module.RERANKER_MODEL_NAME


# ── /embeddings ───────────────────────────────────────────────────────────────

def test_embeddings_empty_input_returns_empty_data_without_touching_any_model(client):
    resp = client.post("/embeddings", json={"input": []})
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_embeddings_defaults_input_type_to_document(main_module):
    req = main_module.EmbeddingsRequest(input=["a"])
    assert req.input_type == "document"


def test_embeddings_document_input_type_uses_passage_embed(main_module, client):
    calls = {}

    class FakeEmbeddingModel:
        def passage_embed(self, texts):
            calls["passage_embed"] = list(texts)
            return [np.array([0.1, 0.2])]

        def query_embed(self, texts):
            calls["query_embed"] = list(texts)
            return [np.array([0.9, 0.9])]

    main_module._embedding_model = FakeEmbeddingModel()
    resp = client.post("/embeddings", json={"input": ["some code"], "input_type": "document"})
    assert resp.status_code == 200
    assert calls == {"passage_embed": ["some code"]}
    assert resp.json()["data"] == [{"embedding": [0.1, 0.2], "index": 0}]


def test_embeddings_query_input_type_uses_query_embed(main_module, client):
    calls = {}

    class FakeEmbeddingModel:
        def passage_embed(self, texts):
            calls["passage_embed"] = list(texts)
            return [np.array([0.1, 0.2])]

        def query_embed(self, texts):
            calls["query_embed"] = list(texts)
            return [np.array([0.9, 0.9])]

    main_module._embedding_model = FakeEmbeddingModel()
    resp = client.post("/embeddings", json={"input": ["how does auth work"], "input_type": "query"})
    assert resp.status_code == 200
    assert calls == {"query_embed": ["how does auth work"]}
    assert resp.json()["data"] == [{"embedding": [0.9, 0.9], "index": 0}]


def test_embeddings_rejects_malformed_request(client):
    resp = client.post("/embeddings", json={"input_type": "document"})  # missing "input"
    assert resp.status_code == 422


# ── /rerank — shared schema/routing behavior ─────────────────────────────────

def test_rerank_empty_candidates_returns_empty_list_without_touching_any_model(client):
    resp = client.post("/rerank", json={"query": "anything", "candidates": [], "top_n": 5})
    assert resp.status_code == 200
    assert resp.json() == []


def test_rerank_request_schema_defaults_top_n_to_20():
    import main as inference_main
    req = inference_main.RerankRequest(query="q", candidates=[])
    assert req.top_n == 20


def test_rerank_request_rejects_malformed_candidate(client):
    resp = client.post("/rerank", json={
        "query": "q",
        "candidates": [{"id": "a"}],  # missing required "text"/"score" fields
    })
    assert resp.status_code == 422  # FastAPI/pydantic validation error, not a 500


# ── Backend selection (env-var driven, build-time only — never config.yaml) ──

def test_backend_selection_defaults_to_colbert_and_its_default_model(monkeypatch):
    monkeypatch.delenv("RERANKER_BACKEND", raising=False)
    monkeypatch.delenv("RERANKER_MODEL", raising=False)
    import main as inference_main
    importlib.reload(inference_main)
    assert inference_main.BACKEND == "colbert"
    assert inference_main.RERANKER_MODEL_NAME == "answerdotai/answerai-colbert-small-v1"


def test_backend_selection_honours_env_var_and_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("RERANKER_BACKEND", "Cross-Encoder")
    monkeypatch.delenv("RERANKER_MODEL", raising=False)
    import main as inference_main
    importlib.reload(inference_main)
    assert inference_main.BACKEND == "cross-encoder"
    assert inference_main.RERANKER_MODEL_NAME == "Xenova/ms-marco-MiniLM-L-6-v2"


def test_backend_selection_explicit_model_overrides_the_default(monkeypatch):
    monkeypatch.setenv("RERANKER_BACKEND", "colbert")
    monkeypatch.setenv("RERANKER_MODEL", "colbert-ir/colbertv2.0")
    import main as inference_main
    importlib.reload(inference_main)
    assert inference_main.RERANKER_MODEL_NAME == "colbert-ir/colbertv2.0"


# ── Cross-encoder backend logic ──────────────────────────────────────────────

def test_cross_encoder_rerank_sorts_by_score_and_preserves_original_score(main_module):
    class FakeCrossEncoder:
        def rerank(self, query, documents):
            # Second document scores highest, regardless of original Qdrant score.
            return [0.1, 0.9]

    main_module._cross_encoder = FakeCrossEncoder()
    req = main_module.RerankRequest(
        query="q",
        candidates=[
            main_module.Candidate(id="a", text="foo", score=0.99),
            main_module.Candidate(id="b", text="bar", score=0.01),
        ],
        top_n=20,
    )
    results = main_module._rerank_cross_encoder(req)
    assert [r.id for r in results] == ["b", "a"]
    assert results[0].score == 0.9
    assert results[0].original_score == 0.01  # original Qdrant score preserved for debugging


def test_cross_encoder_rerank_respects_top_n(main_module):
    class FakeCrossEncoder:
        def rerank(self, query, documents):
            return [0.5, 0.9, 0.1]

    main_module._cross_encoder = FakeCrossEncoder()
    req = main_module.RerankRequest(
        query="q",
        candidates=[
            main_module.Candidate(id="a", text="x", score=0.1),
            main_module.Candidate(id="b", text="y", score=0.1),
            main_module.Candidate(id="c", text="z", score=0.1),
        ],
        top_n=2,
    )
    results = main_module._rerank_cross_encoder(req)
    assert len(results) == 2
    assert [r.id for r in results] == ["b", "a"]  # top 2 by score, descending


# ── ColBERT backend logic ─────────────────────────────────────────────────────

def test_colbert_rerank_uses_maxsim_and_preserves_original_score(main_module):
    class FakeColbertModel:
        def query_embed(self, queries):
            return [np.array([[1.0, 0.0]])]

        def passage_embed(self, texts):
            # "a" barely aligned with the query; "b" aligned perfectly.
            return [np.array([[0.1, 0.9]]), np.array([[1.0, 0.0]])]

    main_module._colbert_model = FakeColbertModel()
    req = main_module.RerankRequest(
        query="q",
        candidates=[
            main_module.Candidate(id="a", text="weak match", score=0.5),
            main_module.Candidate(id="b", text="strong match", score=0.2),
        ],
        top_n=20,
    )
    results = main_module._rerank_colbert(req)
    assert [r.id for r in results] == ["b", "a"]
    assert results[1].original_score == 0.5  # original Qdrant score preserved for "a"


def test_colbert_rerank_falls_back_to_original_order_on_error(main_module):
    class BrokenColbertModel:
        def query_embed(self, queries):
            raise RuntimeError("model not ready")

    main_module._colbert_model = BrokenColbertModel()
    req = main_module.RerankRequest(
        query="q",
        candidates=[
            main_module.Candidate(id="a", text="x", score=0.7),
            main_module.Candidate(id="b", text="y", score=0.3),
        ],
        top_n=20,
    )
    results = main_module._rerank_colbert(req)
    assert [r.id for r in results] == ["a", "b"]  # original order preserved
    assert [r.score for r in results] == [0.7, 0.3]
