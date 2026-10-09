FROM python:3.12-slim

WORKDIR /app

# Install requirements (FastEmbed/onnxruntime only — no torch, no
# sentence-transformers; see requirements.txt for why).
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Re-ranker backend and model — set at build time for pre-download.
# Not overridable at runtime and never a config.yaml field: this is a
# deliberate source-level/build-time choice, not a user-facing config
# knob (see main.py module docstring). Default: ColBERT with the
# code-tuned answerai-colbert-small-v1 model (~130 MB).
# Switch to cross-encoder backend by rebuilding with:
#   docker build --build-arg RERANKER_BACKEND=cross-encoder ...
ARG  RERANKER_BACKEND=colbert
ARG  RERANKER_MODEL=answerdotai/answerai-colbert-small-v1
ENV  RERANKER_BACKEND=${RERANKER_BACKEND}
ENV  RERANKER_MODEL=${RERANKER_MODEL}

COPY maxsim.py .
COPY main.py .

# Pre-download both models at build time so first requests are instant —
# the embedding model is always needed; the re-ranker model depends on
# RERANKER_BACKEND above.
RUN python3 - <<'PYEOF' || echo "Model pre-download failed — will download on first request."
import os
from fastembed import TextEmbedding, LateInteractionTextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

TextEmbedding("nomic-ai/nomic-embed-text-v1.5-Q")

backend = os.getenv("RERANKER_BACKEND", "colbert")
model   = os.getenv("RERANKER_MODEL", "")
if backend == "colbert":
    LateInteractionTextEmbedding(model)
else:
    TextCrossEncoder(model)
PYEOF

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
