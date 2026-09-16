"""Optional local sentence embeddings, and an honest answer when there are none.

The previous ranker's only notion of similarity was a TF-IDF cosine between the
whole resume and the whole posting. That comparison is structurally broken: a
2,000-token resume against a 300-token posting produces a small cosine no
matter how well they match, so the 30 points it controlled moved within a band
of roughly 2 to 10 and decided nothing.

The replacement compares *parts*: each thing the posting asks for against each
thing the candidate has actually done, taking the best match per requirement.
That is what makes "implement non-blocking consumers" find "built an event
ingestion service with Python asyncio" without the two sharing a keyword.

The model is optional. `load_embedder()` returns None when fastembed is not
installed, and the pipeline falls back to BM25 alone rather than failing or
pretending — `available` is reported to the UI so a weaker ranking is never
presented as a semantic one.
"""
from __future__ import annotations

import hashlib
from typing import Optional, Protocol, Sequence

from . import config


class Embedder(Protocol):
    name: str

    def encode(self, texts: Sequence[str]) -> "object":
        """Return an (n, d) float32 matrix of L2-normalised row vectors."""


class OnnxEmbedder:
    """fastembed/ONNX backend: CPU-only, offline after the first download."""

    def __init__(self, model_name: str = config.EMBEDDING_MODEL, store=None):
        from fastembed import TextEmbedding  # imported lazily: optional dependency

        config.EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(config.EMBEDDING_CACHE_DIR))
        self._store = store
        if store is not None:
            store.ensure_embedding_table()
        self._memory: dict[str, bytes] = {}

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.name}\x00{text}".encode("utf-8")).hexdigest()

    def encode(self, texts: Sequence[str]):
        import numpy as np

        texts = list(texts)
        if not texts:
            return np.zeros((0, 1), dtype="float32")
        keys = [self._key(text) for text in texts]
        cached = dict(self._memory)
        missing = [key for key in keys if key not in cached]
        if missing and self._store is not None:
            cached.update(self._store.embeddings(missing))
        pending = [(key, text) for key, text in zip(keys, texts) if key not in cached]
        # De-duplicate before paying for inference: repeated skill lines and
        # boilerplate requirements are common across postings.
        unique_pending = dict(pending)
        if unique_pending:
            vectors = list(self._model.embed(list(unique_pending.values())))
            fresh = {key: np.asarray(vector, dtype="float32").tobytes()
                     for key, vector in zip(unique_pending.keys(), vectors)}
            cached.update(fresh)
            self._memory.update(fresh)
            if self._store is not None:
                self._store.put_embeddings(fresh)
        matrix = np.vstack([np.frombuffer(cached[key], dtype="float32") for key in keys])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.maximum(norms, 1e-12)


def load_embedder(store=None, model_name: str = config.EMBEDDING_MODEL) -> Optional[Embedder]:
    """The embedder if one can be loaded, otherwise None. Never raises."""
    try:
        return OnnxEmbedder(model_name, store=store)
    except Exception:
        # Missing optional dependency, missing model download, or no network on
        # first use. Retrieval degrades to lexical-only and says so.
        return None


def available(store=None) -> bool:
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True
