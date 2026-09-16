"""The cross-encoder stage of the cascade."""
import math

import pytest

from conftest import NOW, RESUME, job
from sww.embedding import load_embedder
from sww.match.pipeline import cascade, rerank_query, rerank_relevance, rerank_scores
from sww.rerank import available, load_reranker
from sww.resume import build_profile

BACKEND = job("backend", "Event Platform Engineering",
              "Implement non-blocking consumers, recover gracefully from transient faults, "
              "and ensure messages are processed without duplicate side effects.")
LAB = job("lab", "Laboratory Technician", "Prepare cell cultures and run PCR assays in a wet lab.")


class StubReranker:
    """Scores by a fixed table, so cascade wiring is testable without a model."""
    name = "stub"

    def __init__(self, table=None, explode=False):
        self.table = table or {}
        self.explode = explode
        self.calls = 0

    def score(self, query, documents):
        self.calls += 1
        if self.explode:
            raise RuntimeError("model unavailable")
        return [self.table.get(index, 0.0) for index, _ in enumerate(documents)]


@pytest.mark.parametrize("logit,expected", [(0.0, 0.5), (30.0, 1.0), (-30.0, 0.0)])
def test_relevance_is_the_models_own_calibrated_probability(logit, expected):
    assert rerank_relevance(logit) == pytest.approx(expected, abs=1e-3)


def test_relevance_is_monotonic_and_bounded():
    values = [rerank_relevance(x) for x in (-50, -5, -1, 0, 1, 5, 50)]
    assert values == sorted(values)
    assert all(0.0 <= value <= 1.0 for value in values)
    # No overflow on an extreme logit.
    assert math.isfinite(rerank_relevance(1e9)) and math.isfinite(rerank_relevance(-1e9))


async def test_query_is_built_from_experience_and_fits_the_input_window():
    analysis = await build_profile(RESUME, now=NOW)
    query = rerank_query(analysis)
    assert "durable queues" in query
    assert len(query) <= 1200


async def test_a_failing_reranker_costs_the_ordering_not_the_run():
    analysis = await build_profile(RESUME, now=NOW)
    broken = StubReranker(explode=True)
    ordered, promise, scores, _ = cascade(analysis, [BACKEND, LAB], reranker=broken)
    assert broken.calls == 1
    assert scores == {}                       # reported as "no rerank happened"
    assert {job["id"] for job in ordered} == {"backend", "lab"}


async def test_a_mismatched_score_count_is_rejected():
    analysis = await build_profile(RESUME, now=NOW)

    class Short(StubReranker):
        def score(self, query, documents):
            return [1.0]

    assert rerank_scores(Short(), analysis, [BACKEND, LAB]) == {}


async def test_rerank_decides_the_order_of_the_pool():
    analysis = await build_profile(RESUME, now=NOW)
    # Score the lab job above the backend job and check the order follows.
    ordered, _, scores, _ = cascade(analysis, [BACKEND, LAB],
                                    reranker=StubReranker({0: -5.0, 1: 5.0}))
    assert len(scores) == 2
    assert [job["id"] for job in ordered][0] == "lab"


async def test_postings_outside_the_pool_keep_retrieval_order_and_sit_below():
    analysis = await build_profile(RESUME, now=NOW)
    postings = [job(str(index), "Backend Developer", "Durable message queues and Python.")
                for index in range(5)]
    reranker = StubReranker({0: 1.0, 1: 2.0})
    ordered, _, scores, _ = cascade(analysis, postings, reranker=reranker, pool=2)
    assert len(scores) == 2
    assert {job["id"] for job in ordered[:2]} == set(scores)


@pytest.mark.skipif(not available(), reason="Install the embeddings extra to run the reranker")
async def test_the_real_cross_encoder_separates_related_from_unrelated_work():
    analysis = await build_profile(RESUME, now=NOW)
    scores = rerank_scores(load_reranker(), analysis, [BACKEND, LAB])
    assert scores["backend"] > scores["lab"]


@pytest.mark.skipif(not available(), reason="Install the embeddings extra to run the reranker")
async def test_the_cascade_improves_on_retrieval_alone_for_a_paraphrased_posting():
    """The end-to-end point of the stage: a posting that names none of the
    candidate's tools but describes their work should end up on top."""
    analysis = await build_profile(RESUME, now=NOW)
    stuffed = job("stuffed", "Marketing Coordinator",
                  "Promote our Python, SQL and Docker developer tools on social media.")
    ordered, _, scores, _ = cascade(analysis, [stuffed, BACKEND],
                                    embedder=load_embedder(), reranker=load_reranker())
    assert scores
    assert [job["id"] for job in ordered][0] == "backend"
