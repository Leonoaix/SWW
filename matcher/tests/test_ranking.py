from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sww.ranking import _deadline_past, rank_jobs


def job(identifier, title="Software Developer", description="Python, SQL, Docker", **fields):
    return {"id": identifier, "title": title, "company": "Example", "location": "Waterloo, ON",
            "description": description, "requirements": "", "deadline": "2099-12-31",
            "url": "https://waterlooworks.uwaterloo.ca/job/%s" % identifier, **fields}


def test_relevant_posting_ranks_first_and_explains_missing_mentions():
    jobs = [job("mechanical", "Mechanical Engineering", "SolidWorks CAD ANSYS manufacturing"),
            job("software", requirements="AWS is a nice-to-have.")]
    result = rank_jobs("Software developer with Python, SQL and Docker experience.", jobs)
    first = result["jobs"][0]
    assert first["id"] == "software"
    assert first["matched_skills"] == ["Docker", "Python", "SQL"]
    assert first["missing_skills"] == ["AWS"]
    assert any("may be optional" in warning for warning in first["warnings"])
    assert first["score"] == round(sum(first["score_breakdown"].values()), 2)
    assert 0 <= first["score"] <= 100
    assert "not a hiring probability" in result["method"]


def test_caps_at_top_hundred_and_reports_entire_pool():
    result = rank_jobs("Python", [job(str(index)) for index in range(125)], limit=1000)
    assert len(result["jobs"]) == 100
    assert result["total_jobs"] == result["eligible_jobs"] == 125
    assert [item["rank"] for item in result["jobs"]] == list(range(1, 101))


def test_small_pool_returns_only_real_jobs():
    result = rank_jobs("Python", [job("1"), job("2")])
    assert len(result["jobs"]) == 2
    assert result["eligible_jobs"] == 2


def test_no_overlap_has_zero_score_not_arbitrary_baseline():
    first = rank_jobs("Botanical microscopy chlorophyll", [job("1", "Astronaut", "Orbital navigation")])["jobs"][0]
    assert first["score"] == 0
    assert first["matched_skills"] == []
    assert any("No matching evidence" in warning for warning in first["warnings"])


def test_missing_skills_do_not_confuse_java_and_javascript():
    first = rank_jobs("JavaScript React", [job("1", "Frontend Developer", "JavaScript React Java")])["jobs"][0]
    assert first["matched_skills"] == ["JavaScript", "React"]
    assert first["missing_skills"] == ["Java"]


def test_job_input_is_not_mutated_and_metadata_survives():
    jobs = [job("1", metadata={"source": "authenticated crawler"})]
    previous = deepcopy(jobs)
    result = rank_jobs("Python", jobs)
    assert jobs == previous
    assert result["jobs"][0]["metadata"] == {"source": "authenticated crawler"}
    assert not any(key.startswith("_ranking") for key in result["jobs"][0])


def test_duplicates_are_removed_with_richer_details_retained():
    jobs = [job("1", description=""), job("1", description="Python SQL Docker AWS"), job("2")]
    result = rank_jobs("Python SQL", jobs)
    assert result["total_jobs"] == 3
    assert result["eligible_jobs"] == 2
    assert len(result["excluded_jobs"]) == 1
    first = next(item for item in result["jobs"] if item["id"] == "1")
    assert first["description"] == "Python SQL Docker AWS"


def test_deduplicates_without_identifiers_by_url_or_identity():
    result = rank_jobs("Python", [job(None, url="https://example.test/job?id=3#first"),
                                  job(None, url="https://example.test/job?id=3#second"),
                                  job(None, title="Other", url=""), job(None, title="Other", url="")])
    assert result["eligible_jobs"] == 2
    assert len(result["excluded_jobs"]) == 2


@pytest.mark.parametrize("fields", [{"status": "Closed"}, {"metadata": {"posting_status": "Expired"}},
                                    {"is_open": False}, {"metadata": {"is_open": False}},
                                    {"deadline": "2000-01-01"}, {"deadline": "January 1, 2000 11:59 PM"}])
def test_closed_and_expired_postings_are_excluded(fields):
    result = rank_jobs("Python", [job("closed", **fields), job("open")])
    assert [item["id"] for item in result["jobs"]] == ["open"]
    assert len(result["excluded_jobs"]) == 1


def test_closed_duplicate_does_not_become_open_when_richer():
    result = rank_jobs("Python", [job("1", status="closed"), job("1", description="Python " * 20)])
    assert result["jobs"] == []
    assert len(result["excluded_jobs"]) == 2


def test_unknown_deadline_is_retained_with_warning():
    first = rank_jobs("Python", [job("1", deadline="Rolling until filled")])["jobs"][0]
    assert any("could not be parsed" in warning for warning in first["warnings"])


def test_toronto_date_only_deadline_remains_open_through_today():
    now = datetime(2026, 9, 14, 23, 59, 59, tzinfo=ZoneInfo("America/Toronto"))
    assert _deadline_past("2026-09-14", now)[0] is False
    assert _deadline_past("2026-09-13", now)[0] is True
    assert _deadline_past("Sep 14, 2026 11:59 PM EDT", now)[0] is True
    assert _deadline_past("2026-09-15T04:01:00Z", now)[0] is False


def test_expired_timestamp_is_excluded_even_if_date_is_today():
    timestamp = datetime.now(ZoneInfo("America/Toronto")) - timedelta(minutes=10)
    result = rank_jobs("Python", [job("1", deadline=timestamp.isoformat())])
    assert result["jobs"] == []


def test_preferences_are_explained_and_exclusion_is_word_bounded():
    jobs = [job("1", "Backend Developer", location="Toronto, ON"),
            job("2", "Frontend Developer", location="Waterloo, ON"),
            job("3", "Senior Backend Developer", location="Toronto, ON")]
    result = rank_jobs("Python", jobs, preferences={"target_roles": ["Backend Developer"],
                                                   "locations": ["Toronto"], "exclude_keywords": ["Senior"]})
    assert result["jobs"][0]["id"] == "1"
    assert result["jobs"][0]["score_breakdown"]["location_preference"] == 5
    assert result["jobs"][0]["score_breakdown"]["role_alignment"] == 10
    assert result["jobs"][1]["score_breakdown"]["role_alignment"] == 0
    assert result["eligible_jobs"] == 2
    assert "Senior" in result["excluded_jobs"][0]["reason"]
    assert "5 for preferred location" in result["method"]


def test_role_preference_accepts_equivalent_title_without_losing_specialization():
    jobs = [job("frontend", "Front-end Developer"), job("backend", "Backend Engineer")]
    result = rank_jobs("Python", jobs, preferences={"target_roles": ["Backend Developer"]})
    assert result["jobs"][0]["id"] == "backend"
    assert result["jobs"][0]["score_breakdown"]["role_alignment"] == 10
    assert result["jobs"][1]["score_breakdown"]["role_alignment"] == 0


def test_ties_are_stable_across_input_order():
    jobs = [job("c"), job("a"), job("b")]
    first = [item["id"] for item in rank_jobs("Python", jobs)["jobs"]]
    second = [item["id"] for item in rank_jobs("Python", list(reversed(jobs)))["jobs"]]
    assert first == second == ["a", "b", "c"]


def test_no_postings_returns_clear_empty_result():
    result = rank_jobs("Python", [])
    assert result["jobs"] == result["excluded_jobs"] == []
    assert result["total_jobs"] == result["eligible_jobs"] == 0


def test_nontechnical_resume_and_job_match():
    result = rank_jobs("Financial analyst with Excel, financial modeling, accounting and valuation.",
                       [job("tech"), job("finance", "Financial Analyst", "Excel financial modeling accounting valuation")])
    assert result["jobs"][0]["id"] == "finance"
    assert set(result["jobs"][0]["matched_skills"]) == {"Excel", "Financial modeling", "Accounting", "Valuation"}


def test_title_only_posting_has_missing_details_warning():
    first = rank_jobs("Python", [job("1", "Python Developer", "")])["jobs"][0]
    assert any("details are missing" in warning for warning in first["warnings"])


@pytest.mark.parametrize("resume,jobs,kwargs", [
    ("", [], {}), (None, [], {}), ("Python", "invalid", {}), ("Python", ["invalid"], {}),
    ("Python", [], {"limit": 0}), ("Python", [], {"limit": True}),
    ("Python", [], {"preferences": {"locations": "Toronto"}}),
])
def test_rejects_invalid_inputs(resume, jobs, kwargs):
    with pytest.raises(ValueError):
        rank_jobs(resume, jobs, **kwargs)
