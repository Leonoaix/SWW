"""Optional local cross-encoder reranking — the cheap middle of the cascade.

Retrieval compares two vectors that were computed without ever seeing each
other. A cross-encoder reads the resume and one posting *together* and scores
that pair directly, which is strictly more information and reliably a better
ordering — at the cost of one model pass per posting instead of one dot
product, about 38 ms each on a laptop CPU.

That cost is why this is a middle stage rather than a replacement for
retrieval: it runs over the few hundred postings retrieval liked, not over
every posting on the board. What it buys is that the expensive, evidence-
grounded model stage downstream spends its budget on a better-chosen set.

Like embeddings it is optional. Without the extra installed, `load_reranker`
returns None and the pipeline keeps the retrieval ordering, reporting that it
did so rather than implying a rerank happened.
"""
from __future__ import annotations

from typing import Optional, Protocol, Sequence

from . import config


class Reranker(Protocol):
    name: str

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """Relevance of each document to the query. Higher is better."""


class CrossEncoderReranker:
    def __init__(self, model_name: str = config.RERANK_MODEL):
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # optional dependency

        config.EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.name = model_name
        self._model = TextCrossEncoder(model_name=model_name, cache_dir=str(config.EMBEDDING_CACHE_DIR))

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        documents = list(documents)
        if not documents:
            return []
        return [float(value) for value in self._model.rerank(query, documents)]


def load_reranker(model_name: str = config.RERANK_MODEL) -> Optional[Reranker]:
    """The reranker if one can be loaded, otherwise None. Never raises."""
    try:
        return CrossEncoderReranker(model_name)
    except Exception:
        return None


def available() -> bool:
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401
    except ImportError:
        return False
    return True
