"""Rules applied before any scoring: duplicates, closed postings, deadlines,
eligibility, and work-term length.

These used to be spread across the top of `rank_jobs` and duplicated, with
small divergences, at the top of the semantic ranker — so a posting could be
excluded by one engine and ranked by the other. One implementation, both
engines.

Work-term length is handled here rather than left to the model, which is both
expensive and unreliable for something a number answers exactly. A posting
whose *shortest* acceptable term exceeds what the applicant can offer is
excluded outright, with the posting's own wording quoted as the reason.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ..resume.profile import CandidateProfile
from ..text import collapse, flatten, mentions, normalise_durations
from .eligibility import eligibility_exclusion

TORONTO = ZoneInfo("America/Toronto")

CLOSED_STATUSES = frozenset({
    "closed", "expired", "cancelled", "canceled", "filled", "archived",
    "applications closed", "posting closed",
})

_DEADLINE_FORMATS = (
    ("%b %d, %Y %I:%M %p", False), ("%B %d, %Y %I:%M %p", False),
    ("%b %d, %Y %H:%M", False), ("%B %d, %Y %H:%M", False),
    ("%Y-%m-%d %I:%M %p", False), ("%Y/%m/%d %H:%M", False),
    ("%m/%d/%Y %I:%M %p", False), ("%d %b %Y %H:%M", False),
    ("%b %d, %Y", True), ("%B %d, %Y", True),
    ("%Y/%m/%d", True), ("%d %b %Y", True), ("%d-%b-%Y", True),
)

# "8 month work term", "8-month", "8 months minimum".
_TERM_MONTHS = re.compile(r"(?<!\w)(\d{1,2})\s*[-–]?\s*month", re.I)
_TERM_MANDATORY = re.compile(
    r"\b(?:must|required|requires|mandatory|only|need to)\b[^.;\n]{0,60}?\b\d{1,2}\s*[-–]?\s*month"
    r"|\b\d{1,2}\s*[-–]?\s*month[^.;\n]{0,40}?\b(?:is\s+)?(?:required|mandatory|only|minimum)\b", re.I)
_TERM_FLEXIBLE = re.compile(r"\b(?:preferred|preference|ideally|nice to have|or\s+\d{1,2}\s*[-–]?\s*month"
                            r"|open to|flexible|either)\b", re.I)




@dataclass
class Exclusion:
    id: str
    title: str
    reason: str

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "reason": self.reason}


def job_text(job: dict) -> str:
    """Everything about a posting that a rule may read, including metadata."""
    parts = [flatten(job.get(field)) for field in
             ("title", "company", "location", "description", "requirements")]
    metadata = job.get("metadata")
    if isinstance(metadata, dict):
        parts.extend(f"{key}: {flatten(value)}" for key, value in metadata.items()
                     if key not in {"detail_status", "detail_collected_at", "detail_error"})
    return "\n".join(part for part in parts if part)


def is_closed(job: dict) -> bool:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    statuses = [job.get("status"), metadata.get("status"),
                metadata.get("posting_status"), metadata.get("application_status")]
    return (any(collapse(flatten(status)).casefold() in CLOSED_STATUSES for status in statuses)
            or job.get("is_open") is False or metadata.get("is_open") is False)


def deadline_passed(value: object, now: datetime) -> tuple[bool, Optional[str]]:
    """Interpret a date-only deadline through the end of that Toronto day."""
    raw = flatten(value).strip()
    if not raw:
        return False, "No application deadline was captured; verify it in WaterlooWorks."
    text = re.sub(r"\s*\((?:EST|EDT|ET)\)\s*$", "", raw, flags=re.I)
    text = re.sub(r"\s+(?:EST|EDT|ET)$", "", text, flags=re.I)
    text = re.sub(r"\s+at\s+", " ", text, flags=re.I)
    text = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", text, flags=re.I)
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))
    parsed = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    if parsed is None:
        for pattern, only_date in _DEADLINE_FORMATS:
            try:
                parsed = datetime.strptime(text, pattern)
                date_only = only_date
                break
            except ValueError:
                continue
    if parsed is None:
        return False, "Deadline could not be parsed (%s); verify it in WaterlooWorks." % raw
    if date_only:
        return parsed.date() < now.date(), None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TORONTO)
    return parsed < now, None


def required_term_months(job: dict) -> tuple[set[int], bool, str]:
    """Work-term lengths a posting names, whether they are mandatory, and the quote."""
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    sources = [flatten(metadata.get(key)) for key in
               ("Work Term Duration", "work_term_duration", "Work Term", "Level")]
    sources += [flatten(job.get("requirements")), flatten(job.get("description")), flatten(job.get("title"))]
    for source in sources:
        if not source.strip():
            continue
        for sentence in re.split(r"[\n;.](?:\s|$)", normalise_durations(source)):
            months = {int(match.group(1)) for match in _TERM_MONTHS.finditer(sentence)
                      if 1 <= int(match.group(1)) <= 24}
            if not months:
                continue
            mandatory = bool(_TERM_MANDATORY.search(sentence)) and not _TERM_FLEXIBLE.search(sentence)
            return months, mandatory, collapse(sentence)[:300]
    return set(), False, ""


def shortest_term_months(job: dict) -> tuple[Optional[int], str]:
    """The shortest work term a posting will accept, with the quote that says so.

    The *minimum* is what decides whether a four-month student can apply, not
    whether the posting mentions eight months anywhere. "An eight-month
    placement is preferred, but four-month placements are equally eligible"
    accepts four; "an uninterrupted eight-month placement is mandatory" does
    not. Reading the smallest duration in the governing sentence separates the
    two without needing to classify the wording as mandatory or not.
    """
    months, _mandatory, quote = required_term_months(job)
    return (min(months) if months else None), quote


def term_limit(preferences: dict, profile: Optional[CandidateProfile]) -> Optional[int]:
    """The longest work term the applicant will take, or None for no filtering.

    An explicit preference wins; otherwise the resume's own stated availability
    is used, so a resume that says "available for a four-month work term" needs
    no extra configuration. `0` disables the filter entirely.
    """
    value = preferences.get("max_term_months")
    if isinstance(value, bool) or not isinstance(value, int):
        value = None
    if value is not None:
        return None if value <= 0 else value
    if profile is not None and profile.availability.months:
        return max(profile.availability.months)
    return None


def term_exclusion(job: dict, limit: Optional[int]) -> str:
    """Why this posting is out of reach on work-term length, or an empty string."""
    if limit is None:
        return ""
    shortest, quote = shortest_term_months(job)
    if shortest is None or shortest <= limit:
        return ""
    return (f"\u5c97\u4f4d\u6700\u77ed\u5de5\u671f {shortest} \u4e2a\u6708\uff0c\u8d85\u8fc7\u53ef\u63a5\u53d7\u7684 {limit} \u4e2a\u6708"
            f"\uff08\u539f\u6587\uff1a{quote}\uff09\u3002")


def dedupe_key(job: dict) -> tuple:
    identifier = flatten(job.get("id")).strip()
    if identifier:
        return ("id", identifier.casefold())
    url = flatten(job.get("url")).strip()
    if url:
        parts = urlsplit(url)
        return ("url", urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, "")))
    return ("content",) + tuple(collapse(flatten(job.get(key))).casefold()
                                for key in ("title", "company", "location"))


def eligible(jobs: list[dict], preferences: dict, *, profile: Optional[CandidateProfile] = None,
             now: Optional[datetime] = None) -> tuple[list[dict], list[dict]]:
    """Split postings into rankable and excluded, with a reason for each exclusion."""
    now = now or datetime.now(TORONTO)
    exclude_keywords = [term for term in preferences.get("exclude_keywords", []) if term.strip()]
    limit = term_limit(preferences, profile)
    unique: dict[tuple, dict] = {}
    excluded: list[Exclusion] = []
    for job in jobs:
        key = dedupe_key(job)
        if key not in unique:
            unique[key] = dict(job)
            continue
        previous = unique[key]
        # Keep the richer copy when a list row and a detail read describe the
        # same posting, but never lose a closed status by doing so.
        richer = max((previous, job), key=lambda item: len(job_text(item)))
        unique[key] = dict(richer)
        if is_closed(previous) or is_closed(job):
            unique[key]["is_open"] = False
        excluded.append(Exclusion(str(job.get("id") or ""), flatten(job.get("title")), "重复职位。"))

    rankable: list[dict] = []
    for job in unique.values():
        content = job_text(job)
        expired, warning = deadline_passed(job.get("deadline"), now)
        reason = ""
        conflict_note = ""
        if is_closed(job):
            reason = "职位已关闭。"
        elif expired:
            reason = "申请截止日期已过。"
        elif restriction := eligibility_exclusion(job, preferences):
            reason = restriction
        elif hits := [term for term in exclude_keywords if mentions(content, term)]:
            reason = "命中排除关键词：" + "、".join(hits)
        elif blocked := term_exclusion(job, limit):
            reason = blocked
        if reason:
            excluded.append(Exclusion(str(job.get("id") or ""), flatten(job.get("title")), reason))
            continue
        rankable.append({**job, "warnings": [warning] if warning else []})
    return rankable, [item.as_dict() for item in excluded]
