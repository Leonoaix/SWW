"""Deterministic scoring from verified evidence."""
import pytest

from conftest import job
from sww.match.scoring import score_criteria, score_local
from sww.resume.profile import CandidateProfile, SkillClaim


def criterion(status="direct", category="technical", importance="must", requirement="要求"):
    return {"requirement": requirement, "category": category, "importance": importance,
            "status": status, "job_quote": "quoted job text", "resume_quote": "quoted resume text",
            "evidence_ref": "b1", "explanation": "说明"}


def test_full_direct_coverage_scores_one_hundred():
    result = score_criteria(job("1"), [criterion(), criterion(category="experience"),
                                       criterion(category="domain")], {})
    assert result["score"] == 100.0
    assert result["must_have_coverage"] == 1.0


def test_no_evidence_scores_zero():
    assert score_criteria(job("1"), [criterion("missing")], {})["score"] == 0.0


@pytest.mark.parametrize("status,expected", [("direct", 1.0), ("transferable", 0.6),
                                             ("missing", 0.0), ("unknown", 0.0)])
def test_transferable_evidence_earns_real_but_lesser_credit(status, expected):
    result = score_criteria(job("1"), [criterion(status)], {})
    assert result["score"] == pytest.approx(100 * expected, abs=0.01)


def test_categories_the_posting_never_states_do_not_penalise_it():
    """A posting with only technical requirements is scored out of technical."""
    technical_only = score_criteria(job("1"), [criterion(category="technical")], {})
    assert technical_only["score"] == 100.0


def test_must_have_requirements_outweigh_preferred_ones():
    mixed = score_criteria(job("1"), [criterion("direct", importance="must", requirement="a"),
                                      criterion("missing", importance="preferred", requirement="b")], {})
    flipped = score_criteria(job("1"), [criterion("missing", importance="must", requirement="a"),
                                        criterion("direct", importance="preferred", requirement="b")], {})
    assert mixed["score"] > flipped["score"]
    assert mixed["must_have_coverage"] == 1.0 and flipped["must_have_coverage"] == 0.0


def test_preference_points_are_only_withheld_when_preferences_exist():
    without = score_criteria(job("1"), [criterion()], {})
    with_unmet = score_criteria(job("1", title="Data Analyst"), [criterion()],
                                {"target_roles": ["Backend Developer"]})
    assert without["score"] == 100.0
    assert with_unmet["score"] == 90.0  # the 10 preference points went unearned
    assert "preferences" not in without["score_breakdown"]


def test_an_eligibility_conflict_caps_the_score():
    criteria = [criterion(), criterion("conflict", category="eligibility", requirement="工期")]
    result = score_criteria(job("1"), criteria, {})
    assert result["score"] == 20.0
    assert result["eligibility"] == "conflict"


def test_the_score_always_equals_its_own_breakdown():
    result = score_criteria(job("1", title="Backend Developer"),
                            [criterion("transferable"), criterion("missing", category="experience")],
                            {"target_roles": ["Backend"]})
    assert result["score"] == pytest.approx(sum(result["score_breakdown"].values()), abs=0.01)
    assert 0 <= result["score"] <= 100


def profile(demonstrated=("Python",), listed=("Rust",)):
    return CandidateProfile(skills=[SkillClaim(name=name, evidence="demonstrated") for name in demonstrated]
                            + [SkillClaim(name=name, evidence="listed") for name in listed])


def test_local_score_normalises_over_present_dimensions_not_to_zero():
    """A posting naming no vocabulary skill loses that dimension, not the run.

    Under the previous ranker this case forfeited 60 of 100 points, which is
    why a posting describing the same work in other words sank to the bottom.
    """
    without_skills = score_local(job("1", description="Improve delivery reliability."),
                                 profile(), [], {}, relevance=0.8, semantic=True)
    assert without_skills["score"] == pytest.approx(80.0, abs=0.01)
    assert "skill_overlap" not in without_skills["score_breakdown"]


def test_local_score_counts_a_listed_only_skill_at_half():
    demonstrated = score_local(job("1"), profile(), ["Python"], {}, 0.0, True)
    listed = score_local(job("1"), profile(), ["Rust"], {}, 0.0, True)
    absent = score_local(job("1"), profile(), ["Kotlin"], {}, 0.0, True)
    assert demonstrated["score"] > listed["score"] > absent["score"]
    assert listed["listed_only_skills"] == ["Rust"]


def test_local_score_warns_when_no_embedding_model_is_installed():
    result = score_local(job("1"), profile(), ["Python"], {}, 0.5, semantic=False)
    assert any("BM25" in warning or "词频" in warning for warning in result["warnings"])


def test_recency_is_scored_from_the_evidence_not_the_posting():
    """The dimension asks "does this match what I have been doing lately",
    which is a fact about the candidate's evidence, not about the job."""
    fresh = score_criteria(job("1"), [{**criterion(), "recency": 1.0}], {})
    stale = score_criteria(job("1"), [{**criterion(), "recency": 0.45}], {})
    assert fresh["score"] > stale["score"]
    assert fresh["recency_alignment"] == 1.0 and stale["recency_alignment"] == 0.45
    # Coverage is identical; only the recency dimension moved.
    assert fresh["score_breakdown"]["technical"] == stale["score_breakdown"]["technical"]


def test_unsupported_criteria_contribute_no_recency():
    scored = score_criteria(job("1"), [{**criterion("direct"), "recency": 1.0},
                                       {**criterion("missing", requirement="b"), "recency": 0.1}], {})
    assert scored["recency_alignment"] == 1.0   # the missing one is not averaged in


def test_must_have_evidence_dominates_the_recency_average():
    scored = score_criteria(job("1"), [
        {**criterion("direct", importance="must", requirement="a"), "recency": 1.0},
        {**criterion("direct", importance="preferred", requirement="b"), "recency": 0.4},
    ], {})
    assert 0.75 < scored["recency_alignment"] < 0.85   # 2:1 weighting


def test_undated_evidence_leaves_the_dimension_unscored():
    scored = score_criteria(job("1"), [criterion()], {})
    assert scored["recency_alignment"] is None
    assert "recency" not in scored["score_breakdown"]
    assert scored["score"] == 100.0      # the budget is not withheld either


def test_local_recency_cannot_earn_points_without_a_match():
    """Scoring recency on its own let a posting that matched nothing collect
    the points for having its nothing attributed to a recent experience."""
    matched = score_local(job("1"), profile(), [], {}, relevance=0.9, semantic=True, recency=1.0)
    unmatched = score_local(job("1"), profile(), [], {}, relevance=0.0, semantic=True, recency=1.0)
    assert unmatched["score"] == 0.0
    assert matched["score_breakdown"]["recency"] > 0
