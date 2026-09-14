"""Deterministic, explainable local ranking. Scores are not hiring probabilities."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .resume import extract_skills, normalize_text


STOP_WORDS = frozenset("""a an and are as at be been being by can co coop co-op company could did do does
each employer employment experience for from full have has had how i if in into
intern internship is it its job jobs join may more must of on one opportunity or
our position qualifications relevant required requirements responsibilities role
seeking skills some student students such team term than that the their them then
there these they this those through to under up us using was we were what when
where which who will with work working would you your years year university
waterloo education resume email phone linkedin github application apply about
ability able strong excellent knowledge understanding including preferred
""".split())

# A role earns points only when its family is explicitly named in the resume
# or the user asks for that role. Skill possession alone does not infer a career.
ROLE_FAMILIES = {
    "software": ("software", "developer", "programmer", "full stack", "full-stack", "backend", "back-end", "frontend", "front-end"),
    "data": ("data analyst", "data analytics", "data scientist", "data science", "data engineer", "business intelligence"),
    "machine_learning": ("machine learning", "deep learning", "artificial intelligence", "ml engineer", "ai engineer"),
    "infrastructure": ("devops", "site reliability", "cloud engineer", "platform engineer", "infrastructure engineer"),
    "security": ("security", "cybersecurity", "penetration test"),
    "testing": ("quality assurance", "test engineer", "software test", "qa engineer", "qa analyst"),
    "hardware": ("electrical", "electronics", "hardware", "fpga", "embedded", "firmware"),
    "mechanical": ("mechanical", "manufacturing", "mechatronics"),
    "civil": ("civil engineering", "civil engineer", "structural engineer", "construction"),
    "design": ("ux", "ui", "user experience", "product design", "graphic design"),
    "product": ("product manager", "product management", "product analyst"),
    "business": ("business analyst", "business analysis", "strategy", "consulting"),
    "finance": ("finance", "financial", "investment", "accounting", "actuarial"),
    "marketing": ("marketing", "copywriting", "communications", "social media"),
    "science": ("laboratory", "biochemistry", "biology", "chemistry", "research assistant"),
}


def _text(value: object) -> str:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value)
    return "" if value is None else str(value)


def _tokens(text: str) -> List[str]:
    return [token for token in (raw.strip(".-") for raw in re.findall(r"[^\W_][\w+#.-]*", text.casefold(), re.UNICODE))
            if len(token) > 1 and token not in STOP_WORDS and not token.isdigit()]


def _mentions(text: str, phrase: str) -> bool:
    return bool(phrase.strip()) and bool(re.search(
        r"(?<!\w)" + re.escape(phrase.casefold().strip()) + r"(?!\w)", text.casefold()))


def _families(text: str) -> set:
    return {family for family, aliases in ROLE_FAMILIES.items()
            if any(_mentions(text, alias) for alias in aliases)}


def _preferred_role_match(title: str, preferences: List[str]) -> bool:
    # Keep user-specified specializations meaningful: requesting a backend role
    # must not award the same bonus to every software occupation.
    def normalized(value: str) -> str:
        value = value.casefold().replace("front-end", "frontend").replace("back-end", "backend")
        return value.replace("full-stack", "full stack")

    title = normalized(title)
    title_tokens = set(_tokens(title))
    generic = {"engineer", "engineering", "developer", "development", "programmer", "junior", "associate"}
    for preference in preferences:
        preference = normalized(preference)
        if _mentions(title, preference):
            return True
        distinctive = set(_tokens(preference)) - generic
        if distinctive and distinctive <= title_tokens:
            return True
    return False


def _vector(tokens: List[str], idf: Dict[str, float]) -> Dict[str, float]:
    return {token: (1 + math.log(count)) * idf.get(token, 1.0)
            for token, count in Counter(tokens).items()}


def _cosine(left: Dict[str, float], right: Dict[str, float]) -> float:
    product = sum(value * right.get(token, 0) for token, value in left.items())
    norm = math.sqrt(sum(v * v for v in left.values()) * sum(v * v for v in right.values()))
    return product / norm if norm else 0.0


def _deadline_past(value: object, now: datetime) -> Tuple[bool, Optional[str]]:
    """Interpret date-only deadlines through the end of that Toronto day."""
    raw = _text(value).strip()
    if not raw:
        return False, "No application deadline was captured; verify it in WaterlooWorks."
    value = re.sub(r"\s*\((?:EST|EDT|ET)\)\s*$", "", raw, flags=re.I)
    value = re.sub(r"\s+(?:EST|EDT|ET)$", "", value, flags=re.I)
    value = re.sub(r"\s+at\s+", " ", value, flags=re.I)
    value = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", value, flags=re.I)
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    parsed = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    formats = (
        ("%b %d, %Y %I:%M %p", False), ("%B %d, %Y %I:%M %p", False),
        ("%b %d, %Y %H:%M", False), ("%B %d, %Y %H:%M", False),
        ("%Y-%m-%d %I:%M %p", False), ("%Y/%m/%d %H:%M", False),
        ("%m/%d/%Y %I:%M %p", False), ("%d %b %Y %H:%M", False),
        ("%b %d, %Y", True), ("%B %d, %Y", True),
        ("%Y/%m/%d", True), ("%d %b %Y", True), ("%d-%b-%Y", True),
    )
    if parsed is None:
        for pattern, is_date in formats:
            try:
                parsed = datetime.strptime(value, pattern)
                date_only = is_date
                break
            except ValueError:
                continue
    if parsed is None:
        return False, "Deadline could not be parsed (%s); verify it in WaterlooWorks." % raw
    if date_only:
        return parsed.date() < now.date(), None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/Toronto"))
    return parsed < now, None


def _dedupe_key(job: dict) -> tuple:
    identifier = _text(job.get("id")).strip()
    if identifier:
        return ("id", identifier.casefold())
    url = _text(job.get("url")).strip()
    if url:
        parsed = urlsplit(url)
        return ("url", urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")))
    return ("content",) + tuple(re.sub(r"\s+", " ", _text(job.get(key))).strip().casefold()
                               for key in ("title", "company", "location"))


def _closed(job: dict) -> bool:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    statuses = [job.get("status"), metadata.get("status"), metadata.get("posting_status"), metadata.get("application_status")]
    return any(_text(status).strip().casefold() in {
        "closed", "expired", "cancelled", "canceled", "filled", "archived", "applications closed", "posting closed",
    } for status in statuses) or job.get("is_open") is False or metadata.get("is_open") is False


def rank_jobs(resume_text: str, jobs: List[dict], limit: int = 100,
              preferences: Optional[dict] = None) -> dict:
    """Return up to 100 jobs with reproducible reasons and explicit limitations.

    Skill points are the fraction of distinct posting skills mentioned in the
    resume. Text points use TF-IDF cosine similarity, with title weighted twice.
    Missing skills mean 'mentioned in posting, absent from resume text', never
    'mandatory' or 'candidate cannot do this'. Unknown deadlines remain eligible.
    """
    if not isinstance(resume_text, str) or not resume_text.strip():
        raise ValueError("Resume text is empty. Upload a readable PDF first.")
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError("Jobs must be a list of job objects.")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Limit must be a positive integer.")
    limit = min(limit, 100)
    if preferences is not None and not isinstance(preferences, dict):
        raise ValueError("Preferences must be an object.")
    preferences = preferences or {}
    preference_lists = {}
    for key in ("target_roles", "locations", "exclude_keywords"):
        values = preferences.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("Preference %s must be a list of strings." % key)
        preference_lists[key] = [value.strip() for value in values if value.strip()]
    now = datetime.now(ZoneInfo("America/Toronto"))
    excluded = []
    unique = {}
    for job in jobs:
        key = _dedupe_key(job)
        if key in unique:
            previous = unique[key]
            # Keep the richer copy when a list-page row and a detail scrape both
            # describe the same posting. Preserve closed/expired status below.
            richer = max((previous, job), key=lambda item: len(_text(item.get("description"))) + len(_text(item.get("requirements"))))
            unique[key] = dict(richer)
            if _closed(previous) or _closed(job):
                unique[key]["is_open"] = False
            excluded.append({"id": job.get("id"), "title": job.get("title", ""), "reason": "Duplicate posting."})
        else:
            unique[key] = dict(job)

    eligible = []
    texts = []
    for job in unique.values():
        content = " ".join(_text(job.get(key)) for key in ("title", "company", "location", "description", "requirements"))
        past, warning = _deadline_past(job.get("deadline"), now)
        reason = None
        if _closed(job):
            reason = "Posting is explicitly closed."
        elif past:
            reason = "Application deadline has passed."
        else:
            excluded_keywords = [keyword for keyword in preference_lists["exclude_keywords"] if _mentions(content, keyword)]
            if excluded_keywords:
                reason = "Excluded by keyword preference: " + ", ".join(excluded_keywords)
        if reason:
            excluded.append({"id": job.get("id"), "title": job.get("title", ""), "reason": reason})
            continue
        job["_ranking_deadline_warning"] = warning
        eligible.append(job)
        texts.append(" ".join(_text(job.get(key)) for key in ("title", "title", "description", "requirements")))

    resume_tokens = _tokens(resume_text)
    documents = [_tokens(content) for content in texts]
    document_frequency = Counter(token for doc in documents + [resume_tokens] for token in set(doc))
    idf = {token: math.log((2 + len(documents)) / (1 + count)) + 1 for token, count in document_frequency.items()}
    resume_vector = _vector(resume_tokens, idf)
    resume_skills = set(extract_skills(resume_text))
    resume_roles = _families(resume_text)
    target_roles = preference_lists["target_roles"]
    locations = preference_lists["locations"]
    skill_weight = 55 if locations else 60
    ranked = []
    for job, content, tokens in zip(eligible, texts, documents):
        warnings = []
        deadline_warning = job.pop("_ranking_deadline_warning")
        if deadline_warning:
            warnings.append(deadline_warning)
        job_skills = set(extract_skills(content))
        matched = sorted(resume_skills & job_skills, key=str.casefold)
        missing = sorted(job_skills - resume_skills, key=str.casefold)
        skill_ratio = len(matched) / len(job_skills) if job_skills else 0.0
        relevance = _cosine(resume_vector, _vector(tokens, idf))
        job_title = _text(job.get("title"))
        job_roles = _families(job_title)
        if target_roles:
            role_match = _preferred_role_match(job_title, target_roles)
        else:
            role_match = bool(job_roles & resume_roles)
        location_match = any(_mentions(_text(job.get("location")), location) for location in locations)
        breakdown = {
            "skill_overlap": round(skill_weight * skill_ratio, 2),
            "text_relevance": round(30 * relevance, 2),
            "role_alignment": 10.0 if role_match else 0.0,
            "location_preference": 5.0 if location_match else 0.0,
        }
        score = round(sum(breakdown.values()), 2)
        reasons = []
        if job_skills:
            reasons.append("Resume mentions %d of %d recognized skills in this posting: %s." % (
                len(matched), len(job_skills), ", ".join(matched) if matched else "none"))
        else:
            warnings.append("No skills from the local vocabulary were recognized in this posting.")
        reasons.append("Resume/posting text similarity: %.1f%% (TF-IDF cosine similarity)." % (100 * relevance))
        if role_match:
            reasons.append("Job title aligns with %s." % ("a preferred role" if target_roles else "a role explicitly mentioned in the resume"))
        if location_match:
            reasons.append("Posting location matches a location preference.")
        if locations and not location_match:
            warnings.append("Posting location did not match the location preferences.")
        if missing:
            warnings.append("Missing skills are posting mentions absent from the resume text; they may be optional and are not verified skill gaps.")
        if not _text(job.get("description")).strip() and not _text(job.get("requirements")).strip():
            warnings.append("Posting details are missing; this ranking is based on the title only.")
        if score == 0:
            warnings.append("No matching evidence was found; this posting is included only because it is available.")
        warnings.append("Eligibility, work authorization, term length, and mandatory criteria need your review in the original posting.")
        ranked.append({**job, "score": score, "matched_skills": matched, "missing_skills": missing,
                       "reasons": reasons, "warnings": warnings, "score_breakdown": breakdown})

    ranked.sort(key=lambda job: (-job["score"], _text(job.get("company")).casefold(),
                                 _text(job.get("title")).casefold(), _text(job.get("id")), _text(job.get("url"))))
    selected = [{**job, "rank": index} for index, job in enumerate(ranked[:limit], 1)]
    return {
        "jobs": selected, "total_jobs": len(jobs), "eligible_jobs": len(eligible), "excluded_jobs": excluded,
        "method": (
            "Local heuristic score (0–100), not a hiring probability: %d points for explicitly mentioned skill coverage, "
            "30 for TF-IDF cosine text similarity, 10 for role alignment%s. Missing skills are mentions, not mandatory requirements. "
            "Closed, expired, excluded-keyword, and duplicate postings are removed. Unknown deadlines remain eligible with a warning."
        ) % (skill_weight, ", 5 for preferred location" if locations else ""),
    }
