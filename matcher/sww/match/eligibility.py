"""Shared, local eligibility exclusions applied before either ranking engine."""

import re


IDENTITY = r"(?:citizenship|(?:Canadian |U\.?S\.? |American )?citizens?|permanent residen(?:ts?|cy|ce)|PR(?: status)?)"
MANDATORY = re.compile(
    r"\b(?:must|shall|need to|are required to|is required to)\s+"
    r"(?:be|have|hold|possess)\s+(?:(?:a|an|the|either|valid|Canadian|U\.?S\.?)\s+){0,3}"
    + IDENTITY + r"\b"
    r"|\b(?:require(?:s|d)? that|only (?:accept|consider|hire)|restricted to|limited to|open only to)\s+"
    r"(?:(?:applicants?|candidates?|students?|who|are|be|is|a|an|either|to)\s+){0,7}"
    + IDENTITY + r"\b"
    r"|\b(?:must|shall|are required to)\s+(?:be able to\s+)?(?:provide|show|present)\s+"
    r"(?:valid\s+)?(?:documentation|proof|evidence)\s+(?:\w+\s+){0,7}"
    + IDENTITY + r"\b"
    r"|\b" + IDENTITY + r"\b\s*(?:status\s+)?(?:(?:is|are)\s+)?(?:required|mandatory|only)\b",
    re.I,
)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            # Preserve structured fields such as {"Citizenship": "Required"}.
            if isinstance(item, str):
                yield f"{key}: {item}"
            else:
                yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def eligibility_exclusion(job: dict, preferences: dict) -> str:
    """SWPP is an explicit keyword preference; identity needs mandatory wording.

    This checks posting text, never infers the applicant's immigration status.
    Unknown or preferred-only identity requirements remain eligible.
    """
    if not preferences.get("exclude_restricted_eligibility", True):
        return ""
    for field in ("title", "description", "requirements", "metadata"):
        for text in _strings(job.get(field)):
            text = re.sub(r"https?://\S+", "", text)
            text = re.sub(r"\bU\.S\.(?:A\.)?", "US", text, flags=re.I)
            if re.search(r"\bSWPP\b|\bStudent Work Placement Program(?:me)?\b", text, re.I):
                return "命中 SWPP（Student Work Placement Program）排除条件。"
            # Newlines/sentences prevent an unrelated 'required' from turning
            # a hiring preference or equal-opportunity statement into a rule.
            for clause in re.split(r"[\n;.!?。；]+|\bbut\b", text, flags=re.I):
                clause = re.sub(r"\s+", " ", clause.replace(":", " ")).strip()
                if re.search(r"\bnot\b|\b(?:preferred|preference|encouraged)\b", clause, re.I):
                    continue
                if re.search(r"\b(?:no|without)\s+(?:\w+\s+){0,2}" + IDENTITY + r"\b", clause, re.I):
                    continue
                # Explicit alternative eligibility must remain available.
                if re.search(r"\bor\b.*\b(?:work permits?|international students?|student visas?)\b", clause, re.I):
                    continue
                match = MANDATORY.search(clause)
                if match:
                    return "岗位明确要求公民／PR 身份：" + match.group(0)
    return ""
