"""Citizenship and SWPP rules. One implementation, used by both engines."""
from copy import deepcopy

import pytest

from conftest import RESUME, FakeClient, job
from sww.llm import Extractor
from sww.match.filters import eligible
from sww.match.pipeline import SemanticRanker, rank_local
from sww.resume import build_profile


async def local(jobs, preferences=None):
    return rank_local(await build_profile(RESUME), jobs, preferences or {})


@pytest.mark.parametrize("text", [
    "This job is funded by SWPP or Canada Summer Jobs.",
    "Funded by swpp.",
    "Student Work Placement Program funding applies.",
    "Applicants must be Canadian citizens or permanent residents.",
    "You must have Canadian citizenship.",
    "Applicants must be a permanent resident.",
    "Candidates must have PR status.",
    "Canadian citizenship is required.",
    "Permanent residents only.",
    "This position is restricted to Canadian citizens and permanent residents.",
    "Due to the subsidy requirements for this hiring, we can only consider applicants who are Canadian citizens or Permanent Residents (PR) for this position.",
    "These programs require that candidates be a Canadian citizen, permanent resident or a protected person.",
    "U.S. citizenship is required.",
    "Applicants must be able to provide valid documentation to show either Canadian or US citizenship, or Canadian Permanent Resident, and undergo a police check with no criminal background.",
])
@pytest.mark.parametrize("field", ["description", "requirements", "metadata"])
async def test_both_engines_exclude_restricted_postings_before_ranking(text, field):
    fields = {field: {"Special Job Requirements": text} if field == "metadata" else text}
    jobs = [job("restricted", **fields), job("open")]
    before = deepcopy(jobs)
    ranked = await local(jobs)
    rankable, excluded = eligible(jobs, {})
    assert [item["id"] for item in ranked["jobs"]] == [item["id"] for item in rankable] == ["open"]
    assert ranked["excluded_jobs"] == excluded
    assert jobs == before


@pytest.mark.parametrize("text", [
    "Hiring Preference given to Canadian Citizens and Permanent Residents. Security Clearance required. Fingerprinting is required.",
    "Canadian citizenship is preferred; Python is required.",
    "Canadian citizenship is not required.",
    "No Canadian citizenship required.",
    "You do not have to be a Canadian citizen.",
    "We welcome applications regardless of citizenship or permanent residence.",
    "We improve the lives of Canadian citizens. Python is required.",
    "Must be legally authorized to work in Canada.",
    "Canadians, permanent residents, and international visa students are eligible for this funding program.",
    "Applicants must be Canadian citizens or international students with a work permit.",
    "Review PRs and support public relations (PR) campaigns.",
    "Experience with SWPPTools is required.",
    "See https://example.test/swpp/eligibility for unrelated program information.",
])
async def test_preferences_negations_and_unrelated_mentions_remain_eligible(text):
    jobs = [job("open", description=text)]
    assert (await local(jobs))["eligible_jobs"] == 1
    assert len(eligible(jobs, {})[0]) == 1


async def test_a_structured_requirement_field_is_detected():
    assert (await local([job("restricted", metadata={"Citizenship": "Required"})]))["eligible_jobs"] == 0


async def test_disabling_the_filter_preserves_the_other_exclusions():
    jobs = [job("swpp", description="SWPP"), job("citizen", description="Citizenship required"),
            job("senior", title="Senior Developer"), job("closed", is_open=False)]
    preferences = {"exclude_restricted_eligibility": False, "exclude_keywords": ["Senior"]}
    ranked = await local(jobs, preferences)
    rankable, _ = eligible(jobs, preferences)
    assert ({item["id"] for item in ranked["jobs"]}
            == {item["id"] for item in rankable} == {"swpp", "citizen"})


async def test_restricted_postings_never_reach_the_model(store):
    client = FakeClient()
    ranker = SemanticRanker(Extractor(client, store, "test"))
    restricted = job("r", description="SWPP. Canadian citizenship is required.")
    result = await ranker.rank(await build_profile(RESUME), [restricted], {}, refine_pairs=0)
    assert result["jobs"] == [] and result["excluded_jobs"]
    assert client.usage["requests"] == 0
