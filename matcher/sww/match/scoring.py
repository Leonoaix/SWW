"""Turn verified evidence into a number, deterministically.

The scoring this replaces had three problems worth naming, because the shape of
this module is a response to each.

  * The local score was 60 points of "fraction of the posting's recognised
    vocabulary words that also appear in the resume". A posting describing the
    same work in other words scored zero on it; a posting naming one matching
    tool scored full marks. Vocabulary coverage, not fit.
  * The semantic score normalised with `90 * weight / denominator * ... / 0.9`,
    a chain nobody could check by reading. The same arithmetic is written here
    as: coverage per category, then a weighted mean over the categories the
    posting actually states.
  * Both engines produced a bare number. Neither reported how much of the
    *mandatory* half was covered — the one figure a co-op applicant needs.

Nothing here is a hiring probability, and every component is reported
separately so a score can be argued with.
"""
from __future__ import annotations

from typing import Optional, Sequence

from .. import config
from ..resume.profile import CandidateProfile
from ..text import flatten, mentions, unique


def _preference_fraction(job: dict, preferences: dict) -> Optional[float]:
    """How many of the user's stated preferences this posting meets, or None
    when they stated none (in which case the points are not withheld)."""
    checks: list[bool] = []
    if roles := [value for value in preferences.get("target_roles", []) if value.strip()]:
        checks.append(any(mentions(flatten(job.get("title")), role) for role in roles))
    if locations := [value for value in preferences.get("locations", []) if value.strip()]:
        checks.append(any(mentions(flatten(job.get("location")), place) for place in locations))
    if not checks:
        return None
    return sum(checks) / len(checks)


def _coverage(criteria: Sequence[dict], categories: Sequence[str],
              importance: Optional[str] = None) -> tuple[float, float]:
    """(weighted credit, total weight) over the matching criteria."""
    credit = total = 0.0
    for item in criteria:
        if item["category"] not in categories:
            continue
        if importance is not None and item["importance"] != importance:
            continue
        weight = config.IMPORTANCE_WEIGHTS.get(item["importance"], 1.0)
        credit += weight * config.STATUS_CREDIT.get(item["status"], 0.0)
        total += weight
    return credit, total


def _evidence_recency(criteria: Sequence[dict]) -> Optional[float]:
    """Weighted mean recency of the experience that satisfied each requirement.

    Only supported criteria count: a requirement nothing answers says nothing
    about how current the candidate is. Must-haves weigh double, the same as in
    the coverage they feed.
    """
    credit = total = 0.0
    for item in criteria:
        value = item.get("recency")
        if value is None or item["status"] not in {"direct", "transferable"}:
            continue
        weight = config.IMPORTANCE_WEIGHTS.get(item["importance"], 1.0)
        credit += weight * float(value)
        total += weight
    return credit / total if total else None


def score_criteria(job: dict, criteria: list[dict], preferences: dict,
                   warnings: Optional[list[str]] = None,
                   retrieval: Optional[float] = None) -> dict:
    """Score one posting from its verified criteria. Pure and reproducible."""
    warnings = list(warnings or [])
    scored_categories = [name for name in config.CATEGORY_WEIGHTS
                         if any(item["category"] == name for item in criteria)]
    breakdown: dict[str, float] = {}
    coverage_by_category: dict[str, float] = {}
    for name in scored_categories:
        credit, total = _coverage(criteria, (name,))
        coverage_by_category[name] = credit / total if total else 0.0

    preference_fraction = _preference_fraction(job, preferences)
    # How recent the work that actually satisfied this posting's requirements
    # is, weighted the same way the requirements are. None when nothing dated
    # supported it, in which case the dimension is not scored at all rather
    # than scored zero.
    recency = _evidence_recency(criteria)
    # Points are only withheld from the evidence budget when there is something
    # to earn them with.
    evidence_budget = (100
                       - (config.PREFERENCE_POINTS if preference_fraction is not None else 0)
                       - (config.RECENCY_POINTS if recency is not None else 0))
    weight_total = sum(config.CATEGORY_WEIGHTS[name] for name in scored_categories)
    fit = 0.0
    for name in scored_categories:
        share = config.CATEGORY_WEIGHTS[name] / weight_total
        points = evidence_budget * share * coverage_by_category[name]
        breakdown[name] = round(points, 2)
        fit += points
    if not scored_categories:
        warnings.append("未识别出技术/经验/领域要求，分数仅反映资格与偏好项。")

    if recency is not None:
        breakdown["recency"] = round(config.RECENCY_POINTS * recency, 2)
    if preference_fraction is not None:
        breakdown["preferences"] = round(config.PREFERENCE_POINTS * preference_fraction, 2)
    score = round(sum(breakdown.values()), 2)

    must_credit, must_total = _coverage(criteria, tuple(config.CATEGORY_WEIGHTS), "must")
    conflicts = [item for item in criteria if item["status"] == "conflict"]
    eligibility_unknown = any(item["status"] == "unknown" and item["category"] == "eligibility"
                              for item in criteria)
    qualification = ("conflict" if conflicts else
                     "needs_review" if eligibility_unknown else "no_known_conflict")
    if qualification == "conflict":
        capped = min(score, config.CONFLICT_SCORE_CAP)
        if capped < score:
            breakdown["qualification_cap"] = round(capped - score, 2)
            score = capped
        warnings.append(
            "存在有原文依据的资格冲突，分数上限 %d；申请前请核对。" % config.CONFLICT_SCORE_CAP)
    if not any(item["category"] == "eligibility" for item in criteria):
        qualification = "needs_review"
        warnings.append("未完整识别资格条件，工期、专业和工作许可等仍需核对。")

    supported = [item for item in criteria if item["status"] in {"direct", "transferable"}]
    return {
        **job,
        "score": round(score, 2),
        "semantic_score": round(score, 2),
        "score_breakdown": breakdown,
        "criteria": criteria,
        "eligibility": qualification,
        "ai_status": "assessed",
        "matched_skills": [item["requirement"] for item in supported],
        "missing_skills": [item["requirement"] for item in criteria if item["status"] == "missing"],
        "reasons": [item["explanation"] for item in supported]
                   or ["暂无可核对的直接或可迁移经验证据。"],
        "warnings": unique(list(job.get("warnings", [])) + warnings),
        "evidence_coverage": round(len(supported) / len(criteria), 3) if criteria else 0.0,
        # The number an applicant actually acts on: how much of the mandatory
        # half is covered. Absent from every previous version of this scorer.
        "must_have_coverage": round(must_credit / must_total, 3) if must_total else None,
        "must_have_count": int(sum(1 for item in criteria if item["importance"] == "must")),
        "retrieval_score": None if retrieval is None else round(retrieval, 4),
        "recency_alignment": None if recency is None else round(recency, 4),
        "pairwise_reviews": [],
    }


def score_local(job: dict, profile: CandidateProfile, posting_skills: Sequence[str],
                preferences: dict, relevance: float, semantic: bool,
                reranked: bool = False, recency: Optional[float] = None) -> dict:
    """Score without any model call.

    Relevance now carries the weight the vocabulary table used to, because it
    is finally a signal: requirement-level semantic matching, or BM25 when no
    embedding model is installed. Skill overlap supports it rather than
    deciding it, and a posting whose wording matches nothing in the local
    vocabulary simply drops that component instead of scoring zero overall.
    """
    demonstrated = {name.casefold() for name in profile.demonstrated_skills}
    attributed = {name.casefold() for name in profile.attributed_skills}
    listed = {name.casefold() for name in profile.listed_skills}
    matched, applied, partial, missing = [], [], [], []
    for skill in posting_skills:
        key = skill.casefold()
        if key in demonstrated:
            matched.append(skill)
        elif key in attributed:
            applied.append(skill)
        elif key in listed:
            partial.append(skill)
        else:
            missing.append(skill)
    components: dict[str, tuple[float, float]] = {"relevance": (55.0, max(0.0, min(1.0, relevance)))}
    if recency is not None:
        # Not the posting's age — how recent the candidate's matching work is,
        # and only to the extent there is a match at all. Scoring recency on
        # its own let a posting that matched nothing collect the points for
        # having its nothing attributed to a recent experience.
        components["recency"] = (float(config.RECENCY_POINTS_LOCAL),
                                 max(0.0, min(1.0, recency)) * max(0.0, min(1.0, relevance)))
    if posting_skills:
        # Three tiers, because there are three things to say. Named inside the
        # work is full credit. Listed and placed by the model in one specific
        # job is most of it. A name in a skills table and nothing else is half:
        # a real claim, and a weaker one.
        covered = (len(matched) + config.ATTRIBUTED_SKILL_CREDIT * len(applied)
                   + config.LISTED_SKILL_CREDIT * len(partial))
        components["skill_overlap"] = (25.0, covered / len(posting_skills))
    preference_fraction = _preference_fraction(job, preferences)
    if preference_fraction is not None:
        components["preferences"] = (10.0, preference_fraction)

    budget = sum(weight for weight, _ in components.values())
    breakdown = {name: round(100 * weight / budget * value, 2) for name, (weight, value) in components.items()}
    score = round(sum(breakdown.values()), 2)
    source = ("本地 cross-encoder 逐对精算" if reranked
              else "逐条要求与经历的最佳匹配" if semantic
              else "BM25 词频检索，未安装嵌入模型")
    reasons = ["相关度 %.1f%%（%s）。" % (100 * max(0.0, min(1.0, relevance)), source)]
    if recency is not None:
        reasons.append("与之匹配的经历新近度 %.0f%%（越近的经历权重越高，两年半衰）。" % (100 * recency))
    if posting_skills:
        reasons.append("岗位提到的 %d 项词表技能中，简历实践过 %d 项%s%s。" % (
            len(posting_skills), len(matched),
            ("，归于具体经历 %d 项" % len(applied)) if applied else "",
            ("，仅列在技能清单 %d 项" % len(partial)) if partial else ""))
    warnings = list(job.get("warnings", []))
    if not posting_skills:
        warnings.append("岗位文本中没有识别出本地词表内的技能；分数已在其余维度上归一。")
    if not semantic and not reranked:
        warnings.append("未启用本地嵌入模型，相关度仅来自词频检索，语义不同的岗位可能被低估。")
    if missing:
        warnings.append("缺少技能指的是岗位提到而简历未出现的词，可能并非必需，也未经核实。")
    warnings.append("资格、工作许可、工期和强制条件仍需在原岗位中核对。")
    return {
        **job,
        "score": score, "score_breakdown": breakdown,
        "matched_skills": matched, "missing_skills": missing,
        "attributed_skills": applied, "listed_only_skills": partial, "reasons": reasons,
        "warnings": unique(warnings), "criteria": [], "eligibility": "needs_review",
        "ai_status": "local", "evidence_coverage": None, "must_have_coverage": None,
        "must_have_count": None, "retrieval_score": round(relevance, 4),
        "reranked": reranked, "recency_alignment": None if recency is None else round(recency, 4),
        "pairwise_reviews": [],
    }
