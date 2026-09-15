"""Evidence verification: what the model says versus what the resume says."""
import pytest

from conftest import JOB, RESUME, FakeClient, judgement_payload, requirements_payload
from sww.jobs.requirements import (
    JobRequirements, cache_key, extract_requirements, load_cached,
)
from sww.llm import DeepSeekError, Extractor
from sww.match.assess import assess
from sww.match.scoring import score_criteria
from sww.resume import build_profile


async def judge(store, judgements, resume=RESUME, requirements=None):
    analysis = await build_profile(resume)
    client = FakeClient(requirements=requirements or requirements_payload(), judgements=judgements)
    extractor = Extractor(client, store, "test")
    parsed = JobRequirements.model_validate(client.responses["requirements"])
    criteria, warnings, _ = await assess(analysis, JOB, parsed, {}, extractor)
    return criteria, warnings, client


async def test_a_fabricated_resume_quote_earns_nothing(store):
    criteria, warnings, _ = await judge(
        store, judgement_payload("direct", "Ten years of production experience"))
    assert criteria[0]["status"] == "unknown"
    assert criteria[0]["resume_quote"] == ""
    assert warnings
    assert score_criteria(JOB, criteria, {})["score"] == 0.0


@pytest.mark.parametrize("quote", ["Languages: Python, SQL, Rust, Go", "Python, SQL, Rust, Go"])
async def test_a_skills_list_quote_cannot_prove_practical_experience(store, quote):
    """The block a quote lives in decides what it proves — no re-scan of the
    raw text for a `Skills:` heading, which is how this used to be judged."""
    criteria, warnings, _ = await judge(store, judgement_payload("direct", quote))
    assert criteria[0]["status"] == "unknown"
    assert score_criteria(JOB, criteria, {})["score"] == 0.0
    assert any("技能清单" in warning for warning in warnings)


async def test_work_evidence_still_counts_when_the_resume_also_has_a_skills_list(store):
    criteria, _, _ = await judge(store, judgement_payload(
        "direct", "Built asynchronous Python services with durable queues"))
    assert criteria[0]["status"] == "direct"
    assert score_criteria(JOB, criteria, {})["score"] == 100.0


async def test_every_requirement_must_be_judged(store):
    two = requirements_payload()
    two["requirements"].append({"text": "另一项必需要求", "category": "technical",
                                "importance": "must",
                                "quote": "Experience with asynchronous services."})
    with pytest.raises(DeepSeekError, match="校验未通过"):
        await judge(store, judgement_payload(index=1), requirements=two)


async def test_a_conflict_is_only_honoured_for_a_mandatory_eligibility_requirement(store):
    criteria, _, _ = await judge(store, judgement_payload(
        "conflict", "Built asynchronous Python services with durable queues"))
    # The requirement is category "experience", so a conflict is downgraded.
    assert criteria[0]["status"] == "unknown"


async def test_an_unverifiable_job_quote_is_rejected_at_extraction(store):
    client = FakeClient(requirements=requirements_payload(quote="Fabricated requirement text"))
    with pytest.raises(DeepSeekError, match="原文引用|校验未通过"):
        await extract_requirements(JOB, Extractor(client, store, "test"))


async def test_requirement_extraction_is_cached_against_the_posting_alone(store):
    """Editing the resume must not re-extract every posting's requirements."""
    client = FakeClient()
    extractor = Extractor(client, store, "test")
    await extract_requirements(JOB, extractor)
    assert client.usage["requests"] == 1
    await extract_requirements(JOB, extractor)
    assert client.usage["requests"] == 1  # served from cache
    assert extractor.hits == 1


async def test_a_cached_entry_is_re_verified_before_it_is_trusted(store):
    """A cache written under a laxer rule cannot smuggle an unverifiable claim
    into a score: it fails verification and is re-requested, not trusted."""
    client = FakeClient()
    extractor = Extractor(client, store, "test")
    await extract_requirements(JOB, extractor)
    store.put_cached(cache_key(JOB), "requirements",
                     requirements_payload(quote="Text that is not in the posting"))
    before = client.usage["requests"]
    result, cached = await extract_requirements(JOB, extractor)
    assert not cached
    assert client.usage["requests"] > before
    assert result.requirements[0].quote != "Text that is not in the posting"


async def test_requirements_are_readable_without_a_client_or_a_model_name(store):
    """Retrieval and local-only ranking need the same extraction the semantic
    engine cached, so the key names the posting and nothing else."""
    client = FakeClient()
    await extract_requirements(JOB, Extractor(client, store, "test"))
    assert load_cached(store, JOB) is not None
    # A different prompt version and model must still find it.
    assert load_cached(store, JOB).requirements[0].quote in JOB["description"]


async def test_a_cached_extraction_whose_quotes_no_longer_check_out_reads_as_absent(store):
    store.put_cached(cache_key(JOB), "requirements",
                     requirements_payload(quote="Text that is not in the posting"))
    assert load_cached(store, JOB) is None
