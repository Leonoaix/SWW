"""BM25 over the crawled postings — the lexical half of retrieval.

Why not the TF-IDF cosine this replaces: cosine normalises by the *whole*
vector length of both documents, so a long resume compared against a short
posting scores low however well they match, and the resulting band was too
narrow to rank anything. BM25 normalises by document length against the corpus
average (the `b` term) and saturates term frequency (the `k1` term), which is
precisely the pair of corrections that comparison needs.

The query is also built differently. It is not "the resume" — it is the
candidate's *demonstrated* vocabulary at full weight and their merely listed
skills at a fraction, so a keyword list cannot outrank real work.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Iterable, Mapping, Optional, Sequence

from .. import config
from ..text import tokens


class BM25:
    """Okapi BM25. Built once per ranking run over the eligible postings."""

    def __init__(self, documents: Sequence[Sequence[str]],
                 k1: float = config.BM25_K1, b: float = config.BM25_B):
        self.k1 = k1
        self.b = b
        self.documents = [Counter(document) for document in documents]
        self.lengths = [sum(counts.values()) for counts in self.documents]
        self.average_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        frequency: Counter[str] = Counter()
        for counts in self.documents:
            frequency.update(counts.keys())
        total = len(self.documents)
        # Robertson/Sparck-Jones idf with the +0.5 smoothing, floored at zero so
        # a term present in every posting cannot subtract from a score.
        self.idf = {term: max(0.0, math.log(1 + (total - count + 0.5) / (count + 0.5)))
                    for term, count in frequency.items()}

    def scores(self, query: Iterable[str], weights: Optional[Mapping[str, float]] = None) -> list[float]:
        """Score every document against a (optionally term-weighted) query."""
        query_counts = Counter(query)
        results = []
        for counts, length in zip(self.documents, self.lengths):
            score = 0.0
            for term, query_frequency in query_counts.items():
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                denominator = frequency + self.k1 * (1 - self.b + self.b * length / (self.average_length or 1))
                weight = weights.get(term, 1.0) if weights else 1.0
                score += weight * self.idf.get(term, 0.0) * frequency * (self.k1 + 1) / denominator
            results.append(score)
        return results


def weighted_query(demonstrated: Iterable[tuple[str, float]], listed: Iterable[str],
                   listed_weight: float = config.LISTED_SKILL_WEIGHT) -> tuple[list[str], dict[str, float]]:
    """Query terms plus per-term weights.

    `demonstrated` is (text, weight) per experience, so recency carries into
    the lexical half as well as the semantic one. A term used in several
    experiences takes the strongest — most recent — of them, rather than being
    dragged down by having also appeared years ago.
    """
    query: list[str] = []
    weights: dict[str, float] = {}
    for text, weight in demonstrated:
        for term in tokens(text):
            query.append(term)
            weights[term] = max(weights.get(term, 0.0), weight)
    for text in listed:
        for term in tokens(text):
            query.append(term)
            weights.setdefault(term, listed_weight)
    return query, weights
