"""End-to-end ranking, local and semantic."""
from copy import deepcopy

import pytest

from conftest import FakeClient, NOW, RESUME, job, judgement_payload, requirements_payload
from sww.embedding import available, load_embedder
from sww.llm import Extractor
from sww.match.pipeline import (
    SemanticRanker, cached_requirement_chunks, cascade, rank_local,
)
from sww.resume import build_profile

BACKEND = job("backend", "Backend Developer",
              "Develop reliable event-driven services using durable message queues.")
LAB = job("lab", "Laboratory Technician", "Prepare cell cultures and run PCR assays.")


async def rank(jobs, preferences=None, embedder=None, limit=100):
    analysis = await build_profile(RESUME, now=NOW)
    return rank_local(analysis, jobs, preferences or {}, limit, embedder)


async def test_relevant_posting_outranks_an_unrelated_one():
    result = await rank([LAB, BACKEND])
    assert result["jobs"][0]["id"] == "backend"
    assert result["engine"] == "local"


async def test_ranking_caps_at_the_limit_and_reports_the_whole_pool():
    result = await rank([job(str(index)) for index in range(125)], limit=100)
    assert len(result["jobs"]) == 100
    assert result["total_jobs"] == result["eligible_jobs"] == 125
    assert [item["rank"] for item in result["jobs"]] == list(range(1, 101))


async def test_every_score_equals_its_breakdown_and_stays_in_range():
    for item in (await rank([BACKEND, LAB, job("x")]))["jobs"]:
        assert item["score"] == pytest.approx(sum(item["score_breakdown"].values()), abs=0.01)
        assert 0 <= item["score"] <= 100


async def test_job_input_is_not_mutated():
    jobs = [job("1", metadata={"source": "authenticated crawler"})]
    before = deepcopy(jobs)
    await rank(jobs)
    assert jobs == before


async def test_local_ranking_needs_no_model_and_says_which_signals_it_used():
    result = await rank([BACKEND], embedder=None)
    assert result["semantic_retrieval"] is False
    assert any("fastembed" in warning for warning in result["warnings"])
    assert result["usage"] == {}


@pytest.mark.skipif(not available(), reason="Install the embeddings extra to run semantic retrieval")
async def test_paraphrased_posting_is_no_longer_sunk_by_vocabulary_coverage():
    """The failure that motivated this rewrite.

    `paraphrased` describes exactly the candidate's work without naming one of
    their tools. The previous ranker awarded it zero skill points out of sixty
    and ranked it below a marketing posting that merely listed the words.
    """
    paraphrased = job("paraphrased", "Event Platform Engineering",
                      "Implement non-blocking consumers, recover gracefully from transient "
                      "faults, and ensure messages are processed without duplicate side effects.")
    stuffed = job("stuffed", "Marketing Coordinator",
                  "Promote our Python, SQL and Docker developer tools on social media. "
                  "Write campaign copy about Python and SQL.")
    result = await rank([stuffed, paraphrased], embedder=load_embedder())
    order = [item["id"] for item in result["jobs"]]
    assert order.index("paraphrased") < order.index("stuffed")


async def test_semantic_ranking_shortlists_and_reports_what_it_skipped(store):
    analysis = await build_profile(RESUME, now=NOW)
    client = FakeClient(judgements=judgement_payload(
        "direct", "Built asynchronous Python services with durable queues"))
    ranker = SemanticRanker(Extractor(client, store, "test"))
    postings = [job(str(index), "Backend Developer",
                    "Develop reliable event-driven services using durable message queues.")
                for index in range(5)]
    result = await ranker.rank(analysis, postings, {}, limit=100, shortlist=2, refine_pairs=0)
    assert result["shortlisted_jobs"] == 2
    assert result["not_assessed_jobs"] == 3
    assert result["complete"] is False
    assert any("未评估" in warning for warning in result["warnings"])


async def test_shortlist_zero_assesses_everything(store):
    analysis = await build_profile(RESUME, now=NOW)
    client = FakeClient(judgements=judgement_payload(
        "direct", "Built asynchronous Python services with durable queues"))
    ranker = SemanticRanker(Extractor(client, store, "test"))
    postings = [job(str(index), "Backend Developer",
                    "Develop reliable event-driven services using durable message queues.")
                for index in range(3)]
    result = await ranker.rank(analysis, postings, {}, shortlist=0, refine_pairs=0)
    assert result["assessed_jobs"] == 3
    assert result["not_assessed_jobs"] == 0
    assert result["complete"] is True


async def test_restricted_postings_never_reach_the_model(store):
    analysis = await build_profile(RESUME, now=NOW)
    client = FakeClient()
    ranker = SemanticRanker(Extractor(client, store, "test"))
    restricted = job("r", description="SWPP. Canadian citizenship is required.")
    result = await ranker.rank(analysis, [restricted], {}, refine_pairs=0)
    assert result["jobs"] == []
    assert result["excluded_jobs"]
    assert client.usage["requests"] == 0


async def test_a_second_run_reuses_the_cache_and_spends_nothing(store):
    analysis = await build_profile(RESUME, now=NOW)
    client = FakeClient(judgements=judgement_payload(
        "direct", "Built asynchronous Python services with durable queues"))
    postings = [job("1", "Backend Developer",
                    "Develop reliable event-driven services using durable message queues.")]
    first = await SemanticRanker(Extractor(client, store, "test")).rank(
        analysis, postings, {}, refine_pairs=0)
    spent = client.usage["requests"]
    second = await SemanticRanker(Extractor(client, store, "test")).rank(
        analysis, postings, {}, refine_pairs=0)
    assert client.usage["requests"] == spent
    assert [item["id"] for item in first["jobs"]] == [item["id"] for item in second["jobs"]]
    assert second["cache_hits"] > 0


async def test_requirements_extracted_once_feed_the_next_run_s_retrieval(store):
    """The second run compares the resume against the posting's real
    requirements instead of against sentences chopped out of its prose."""
    analysis = await build_profile(RESUME, now=NOW)
    postings = [job("1", "Backend Developer",
                    "Develop reliable event-driven services using durable message queues.")]
    assert cached_requirement_chunks(store, postings) == {}
    assert cascade(analysis, postings, store=store)[3] is False

    client = FakeClient(judgements=judgement_payload(
        "direct", "Built asynchronous Python services with durable queues"))
    await SemanticRanker(Extractor(client, store, "test")).rank(
        analysis, postings, {}, refine_pairs=0)

    chunks = cached_requirement_chunks(store, postings)
    assert chunks["1"][0]["importance"] == "must"
    assert cascade(analysis, postings, store=store)[3] is True


async def test_local_ranking_reports_which_signals_it_actually_used():
    analysis = await build_profile(RESUME, now=NOW)
    postings = [job("1", "Backend Developer", "Durable message queues.")]
    bare = rank_local(analysis, postings, {}, embedder=None, reranker=None)
    assert bare["reranked_jobs"] == 0
    assert bare["requirement_chunks_used"] is False
    assert any("\u91cd\u6392" in warning for warning in bare["warnings"])
