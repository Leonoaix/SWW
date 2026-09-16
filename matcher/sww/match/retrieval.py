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


@dataclass
class RetrievalResult:
    job_id: str
    lexical: float
    semantic: Optional[float]
    fused: float
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
        summary = collapse(f"{item.title} {item.organization} {item.summary}")
        if summary:
            chunks.append(Chunk(summary, 1.0, f"{item.kind}:{item.block_id}"))
        for highlight in item.highlights:
            if collapse(highlight):
                chunks.append(Chunk(collapse(highlight), 1.0, f"{item.kind}:{item.block_id}"))
        if item.technologies:
            chunks.append(Chunk(", ".join(item.technologies), 0.8, f"tech:{item.block_id}"))
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


def _max_similarity(embedder, resume: list[Chunk], jobs: list[list[Chunk]]) -> list[Optional[float]]:
    """Per-job weighted mean of each job chunk's best resume match."""
    import numpy as np

    flat = [chunk.text for group in jobs for chunk in group]
    if not resume or not flat:
        return [None] * len(jobs)
    resume_matrix = embedder.encode([chunk.text for chunk in resume])
    resume_weights = np.asarray([chunk.weight for chunk in resume], dtype="float32")
    job_matrix = embedder.encode(flat)
    # One matrix product for the whole corpus; the max is taken per job chunk.
    similarity = job_matrix @ resume_matrix.T
    # A chunk the candidate only *listed* may support a requirement, but not as
    # strongly as one they demonstrated. Scale before taking the maximum.
    similarity = similarity * resume_weights[None, :]
    best = similarity.max(axis=1)
    scores: list[Optional[float]] = []
    offset = 0
    for group in jobs:
        if not group:
            scores.append(None)
            continue
        window = best[offset:offset + len(group)]
        weights = np.asarray([chunk.weight for chunk in group], dtype="float32")
        offset += len(group)
        scores.append(float((window * weights).sum() / max(weights.sum(), 1e-9)))
    return scores


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
    demonstrated = [item.as_text() for item in profile.experiences] or [analysis.text]
    query, weights = weighted_query(demonstrated, profile.listed_skills)
    lexical = index.scores(query, weights)

    groups = [job_chunks(job, (requirements or {}).get(str(job.get("id")))) for job in jobs]
    semantic: list[Optional[float]] = [None] * len(jobs)
    if embedder is not None:
        try:
            semantic = _max_similarity(embedder, resume_chunks(analysis), groups)
        except Exception:
            semantic = [None] * len(jobs)  # Never let retrieval take the run down.

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
            results.append(RetrievalResult(str(job.get("id") or i), lexical[i], semantic[i], fused))
    else:
        span = max(lexical) or 1.0
        for i, job in enumerate(jobs):
            results.append(RetrievalResult(str(job.get("id") or i), lexical[i], None, lexical[i] / span))
    return results


def shortlist(results: Sequence[RetrievalResult], limit: int) -> list[str]:
    """The highest-scoring job ids, most promising first."""
    return [item.job_id for item in sorted(results, key=lambda item: -item.fused)][:limit]
