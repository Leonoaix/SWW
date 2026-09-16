"""Hybrid retrieval: BM25 fused with requirement-level semantic matching.

The unit of comparison is the point. The old ranker asked "how similar is this
resume to this posting?", which no single number answers well. This asks, for
each thing the posting wants, "what is the closest thing this person has
actually done?" — then aggregates those per-requirement bests, weighting
must-haves above nice-to-haves.

That is a late-interaction (max-sim) score rather than a single dot product
between two document vectors, and it is the reason a posting that describes
backend work without naming a single tool in the resume can still surface.

The two signals are combined with Reciprocal Rank Fusion, which needs no score
calibration between an unbounded BM25 value and a bounded cosine — a recurring
source of silent weighting bugs when people normalise them by hand.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .. import config
from ..resume.profile import CandidateProfile, ResumeAnalysis
from ..text import collapse, flatten, tokens
from .lexical import BM25, weighted_query

RRF_K = 60
_SPLIT = re.compile(r"(?:\n+|(?<=[.;!?。；])\s+)")


@dataclass
class Chunk:
    """One comparable unit of text with the weight it carries in aggregation."""
    text: str
    weight: float = 1.0
    label: str = ""
    # How recent the experience behind this chunk is, when it came from one.
    recency: Optional[float] = None


@dataclass
class RetrievalResult:
    job_id: str
    lexical: float
    semantic: Optional[float]
    fused: float
    # Weighted mean recency of the experiences that actually matched this
    # posting: high when it lines up with recent work, low when the only thing
    # supporting it is years old. None when nothing dated matched.
    recency: Optional[float] = None
    matches: list[tuple[str, float]] = field(default_factory=list)

    @property
    def relevance(self) -> float:
        """A bounded 0-1 relevance, calibrated against constants rather than
        against the best posting in this run.

        The point is that one excellent new posting must not deflate every
        other posting's score, which dividing by the batch maximum would do.
        The semantic half is exactly stable: a fixed floor and ceiling, so the
        same resume and posting give the same number in any batch. The lexical
        half is *nearly* stable — BM25's idf is a corpus statistic by
        definition, so adding postings shifts it slightly — but it is squashed
        by x/(x+k) rather than normalised against the batch, and carries the
        smaller share of the blend. In practice the drift is a fraction of a
        point, not the difference between 40 and 100.
        """
        lexical = self.lexical / (self.lexical + config.BM25_SATURATION)
        if self.semantic is None:
            return round(lexical, 6)
        span = config.SEMANTIC_CEILING - config.SEMANTIC_FLOOR
        semantic = max(0.0, min(1.0, (self.semantic - config.SEMANTIC_FLOOR) / span))
        blend = config.SEMANTIC_BLEND
        return round(blend * semantic + (1 - blend) * lexical, 6)

    @property
    def percent(self) -> float:
        return round(100 * self.relevance, 2)


def resume_chunks(analysis: ResumeAnalysis) -> list[Chunk]:
    """What the candidate has done, one comparable piece at a time.

    Highlights are separate chunks because a single bullet is usually what
    satisfies a single requirement; folding a whole role into one vector
    averages its specifics away.
    """
    profile: CandidateProfile = analysis.profile
    chunks: list[Chunk] = []
    for item in profile.experiences:
        # Recent work weighs more. It is what the candidate is fluent in now
        # and the better signal of what they want next; a four-year-old term
        # still counts, just less.
        weight = item.recency
        summary = collapse(f"{item.title} {item.organization} {item.summary}")
        if summary:
            chunks.append(Chunk(summary, weight, f"{item.kind}:{item.block_id}", weight))
        for highlight in item.highlights:
            if collapse(highlight):
                chunks.append(Chunk(collapse(highlight), weight,
                                    f"{item.kind}:{item.block_id}", weight))
        if item.technologies:
            chunks.append(Chunk(", ".join(item.technologies), 0.8 * weight,
                                f"tech:{item.block_id}", weight))
    if not chunks:
        # No structured experience: fall back to the evidence blocks verbatim
        # rather than silently retrieving on nothing.
        for block in analysis.evidence_blocks():
            chunks.append(Chunk(collapse(block.text)[:1000], 1.0, f"block:{block.id}"))
    # Decided before the skills list is added: a resume whose experience could
    # not be segmented must retrieve on its full text, not on a keyword list.
    # Appending the list first made that fallback unreachable, so such a resume
    # was matched against nothing but its own `Skills:` line.
    if not chunks and analysis.text.strip():
        for piece in _SPLIT.split(analysis.text):
            piece = collapse(piece)
            if len(piece) >= 25:
                chunks.append(Chunk(piece[:600], 1.0, "resume"))
        if not chunks:
            chunks.append(Chunk(collapse(analysis.text)[:2000], 1.0, "resume"))
    if listed := profile.listed_skills:
        chunks.append(Chunk(", ".join(listed), config.LISTED_SKILL_WEIGHT, "skills-list"))
    return chunks[:config.MAX_RESUME_CHUNKS]


def job_chunks(job: dict, requirements: Optional[Sequence[dict]] = None) -> list[Chunk]:
    """What the posting asks for, one requirement at a time.

    Uses extracted requirements when they exist; otherwise splits the posting
    into sentences and bullets, which is still far closer to a requirement than
    the whole document.
    """
    if requirements:
        return [Chunk(collapse(item["text"]),
                      config.IMPORTANCE_WEIGHTS.get(item.get("importance", "preferred"), 1.0),
                      item.get("category", ""))
                for item in requirements if collapse(item.get("text", ""))][:config.MAX_JOB_CHUNKS]
    chunks = [Chunk(collapse(flatten(job.get("title"))), 2.0, "title")] if job.get("title") else []
    body = "\n".join(flatten(job.get(field)) for field in ("description", "requirements"))
    for piece in _SPLIT.split(body):
        piece = collapse(re.sub(r"^\s*(?:[-*•·▪]|\d+[.)])\s*", "", piece))
        if len(piece) >= 25:
            chunks.append(Chunk(piece[:600], 1.0, "body"))
    return chunks[:config.MAX_JOB_CHUNKS]


def _max_similarity(embedder, resume: list[Chunk], jobs: list[list[Chunk]]
                    ) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """Per-job (score, recency) from each job chunk's best resume match.

    The score is the weighted mean of those bests. The recency is the weighted
    mean of *whose* experience won each of them — which is what answers "does
    this posting line up with what I have been doing lately".
    """
    import numpy as np

    flat = [chunk.text for group in jobs for chunk in group]
    if not resume or not flat:
        return [None] * len(jobs), [None] * len(jobs)
    resume_matrix = embedder.encode([chunk.text for chunk in resume])
    resume_weights = np.asarray([chunk.weight for chunk in resume], dtype="float32")
    job_matrix = embedder.encode(flat)
    # One matrix product for the whole corpus; the max is taken per job chunk.
    similarity = job_matrix @ resume_matrix.T
    # Which experience is closest is a question about similarity alone. Taking
    # the argmax after weighting let the weighting pick its own winner: a
    # recent experience outranked a better-matching older one, and then the
    # recency attributed to that posting was the recent one's — circular.
    winner = similarity.argmax(axis=1)
    # A chunk the candidate only *listed* may support a requirement, but not as
    # strongly as one they demonstrated. Scale before taking the maximum.
    best = (similarity * resume_weights[None, :]).max(axis=1)
    recencies = [chunk.recency for chunk in resume]

    scores: list[Optional[float]] = []
    ages: list[Optional[float]] = []
    offset = 0
    for group in jobs:
        if not group:
            scores.append(None)
            ages.append(None)
            continue
        window = best[offset:offset + len(group)]
        chosen = winner[offset:offset + len(group)]
        weights = np.asarray([chunk.weight for chunk in group], dtype="float32")
        offset += len(group)
        scores.append(float((window * weights).sum() / max(weights.sum(), 1e-9)))
        dated = [(recencies[index], float(weight))
                 for index, weight in zip(chosen, weights) if recencies[index] is not None]
        ages.append(sum(value * weight for value, weight in dated) / sum(w for _, w in dated)
                    if dated else None)
    return scores, ages


def _lexical_recency(analysis: ResumeAnalysis, job: dict) -> Optional[float]:
    """Which experience best overlaps this posting, by shared vocabulary.

    The fallback when no embedding model is installed: cruder than the semantic
    argmax, but it keeps the recency dimension available rather than silently
    dropping it from the score.
    """
    posting = set(tokens(flatten(job.get("title")) + " " + flatten(job.get("description"))
                         + " " + flatten(job.get("requirements"))))
    if not posting:
        return None
    best, overlap = None, 0
    for item in analysis.profile.experiences:
        shared = len(posting & set(tokens(item.as_text())))
        if shared > overlap:
            best, overlap = item, shared
    return best.recency if best is not None else None


def retrieve(analysis: ResumeAnalysis, jobs: Sequence[dict], *, embedder=None,
             requirements: Optional[dict[str, list[dict]]] = None) -> list[RetrievalResult]:
    """Score every posting lexically and, when a model is available, semantically."""
    jobs = list(jobs)
    if not jobs:
        return []
    documents = [tokens(" ".join(flatten(job.get(field)) for field in
                                 ("title", "title", "company", "location", "description", "requirements")))
                 for job in jobs]
    index = BM25(documents)
    profile = analysis.profile
    recent = [(item.as_text(), item.recency) for item in profile.experiences]
    query, weights = weighted_query(recent or [(analysis.text, 1.0)], profile.listed_skills)
    lexical = index.scores(query, weights)

    groups = [job_chunks(job, (requirements or {}).get(str(job.get("id")))) for job in jobs]
    semantic: list[Optional[float]] = [None] * len(jobs)
    recency: list[Optional[float]] = [None] * len(jobs)
    if embedder is not None:
        try:
            semantic, recency = _max_similarity(embedder, resume_chunks(analysis), groups)
        except Exception:
            # Never let retrieval take the run down.
            semantic, recency = [None] * len(jobs), [None] * len(jobs)
    if all(value is None for value in recency):
        recency = [_lexical_recency(analysis, job) for job in jobs]

    def ranks(values: Sequence[Optional[float]]) -> list[int]:
        order = sorted(range(len(values)), key=lambda i: -(values[i] if values[i] is not None else -math.inf))
        positions = [0] * len(values)
        for position, i in enumerate(order, start=1):
            positions[i] = position
        return positions

    lexical_ranks = ranks(lexical)
    results = []
    if any(value is not None for value in semantic):
        semantic_ranks = ranks(semantic)
        for i, job in enumerate(jobs):
            fused = 1 / (RRF_K + lexical_ranks[i]) + 1 / (RRF_K + semantic_ranks[i])
            results.append(RetrievalResult(str(job.get("id") or i), lexical[i], semantic[i],
                                           fused, recency[i]))
    else:
        span = max(lexical) or 1.0
        for i, job in enumerate(jobs):
            results.append(RetrievalResult(str(job.get("id") or i), lexical[i], None,
                                           lexical[i] / span, recency[i]))
    return results


def shortlist(results: Sequence[RetrievalResult], limit: int) -> list[str]:
    """The highest-scoring job ids, most promising first."""
    return [item.job_id for item in sorted(results, key=lambda item: -item.fused)][:limit]
