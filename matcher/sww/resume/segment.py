"""Split a resume into labelled blocks before anything tries to score it.

This is the step the previous pipeline skipped entirely. Without it, a resume
is one undifferentiated string, so "Python" in a comma-separated `Skills:` line
and "Python" in "built an ingestion service in Python" are indistinguishable —
and the semantic ranker had to re-derive the difference with a regex that
re-scanned the raw text for a `Skills:` heading (`skills_list_only`). Here the
distinction is a property of the block a span belongs to, decided once.

Everything is deterministic and offline. It runs with no API key, and its
output is what both the local ranker and the model prompt are built from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Literal, Optional

from ..text import clean_unicode, collapse

BlockKind = Literal["header", "summary", "education", "experience", "project",
                    "skills", "award", "publication", "activity", "other"]

# Demonstrated work: a claim inside one of these blocks is backed by something
# the candidate did. A claim inside `skills` is a claim and nothing more.
EVIDENCE_KINDS: frozenset[str] = frozenset({"experience", "project", "publication", "activity"})

_SECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("education", r"education|academic(?:\s+background)?|schooling|教育(?:背景|经历)?|学历"),
    ("experience", r"(?:work|professional|employment|industry|relevant|related)?\s*experience"
                   r"|employment(?:\s+history)?|internships?|co-?op(?:\s+experience)?|positions?\s+held"
                   r"|工作(?:经历|经验)|实习(?:经历|经验)?"),
    ("project", r"(?:technical|personal|academic|selected|side|key)?\s*projects?|portfolio|项目(?:经历|经验)?"),
    ("skills", r"(?:technical|core|professional|key|relevant)?\s*skills(?:\s*(?:&|and)\s*\w+)?"
               r"|technolog(?:y|ies)|tools?(?:\s*(?:&|and)\s*technologies)?|competenc(?:y|ies)"
               r"|programming\s+languages|tech\s+stack|技能|技术(?:技能|栈)?|专业技能"),
    ("award", r"awards?|honou?rs?|achievements?|certifications?|licen[cs]es?|scholarships?|获奖|证书"),
    ("publication", r"publications?|papers?|research|patents?|conferences?|论文|研究"),
    ("activity", r"(?:extracurricular|volunteer|leadership|community)\s*(?:activities|experience|work)?"
                 r"|activities|involvement|clubs?|societ(?:y|ies)|社团|志愿"),
    ("summary", r"summary|objective|profile|about(?:\s+me)?|highlights?|个人简介|自我评价"),
)
_SECTIONS = tuple((kind, re.compile(r"^\W*(?:" + pattern + r")\W*$", re.I)) for kind, pattern in _SECTION_PATTERNS)

# "May 2024 - August 2024", "2023–Present", "Summer 2025", "09/2024 - 12/2024".
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_SEASON = r"(?:spring|summer|fall|autumn|winter)"
_POINT = rf"(?:(?:{_MONTH}|{_SEASON})\.?\s*,?\s*)?(?:\d{{4}}|\d{{1,2}}[/-]\d{{4}})"
_DATE_RANGE = re.compile(rf"(?<!\w)(?:{_POINT})\s*(?:[-–—]|to|至|~)\s*(?:{_POINT}|present|current|now|今)", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•·▪◦‣]|\d+[.)])\s+")
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_SEASON_MONTH = {"winter": 1, "spring": 5, "summer": 5, "fall": 9, "autumn": 9}


@dataclass
class Block:
    """One labelled span of the resume, addressable by id and byte range."""
    id: str
    kind: BlockKind
    heading: str
    text: str
    start: int
    end: int
    bullets: list[str] = field(default_factory=list)
    months: Optional[int] = None

    @property
    def is_evidence(self) -> bool:
        """Whether a claim quoted from this block is backed by demonstrated work."""
        return self.kind in EVIDENCE_KINDS


_INLINE_LABEL = re.compile(
    r"(?<![\w-])(" + "|".join(pattern for _, pattern in _SECTION_PATTERNS) + r")\s*[:：]",
    re.I)


def _split_inline_label(line: str) -> Optional[tuple[BlockKind, str, str]]:
    """Read "Experience: built an ingestion service" as heading plus content.

    Text extracted from a PDF frequently arrives as one long line with inline
    labels instead of real headings. Requiring a heading to occupy its own line
    made every such resume a single unsegmented blob — which is exactly how a
    resume ends up retrieved on its skills list alone.
    """
    match = _INLINE_LABEL.match(line.strip())
    if match is None:
        return None
    kind = _heading_kind(match.group(1))
    if kind is None:
        return None
    return kind, match.group(1).strip(), line.strip()[match.end():].lstrip()


def _normalise_layout(text: str) -> str:
    """Give inline section labels their own line so one code path handles both."""
    lines = []
    for line in text.splitlines():
        if len(line) <= 120:
            lines.append(line)
            continue
        # Only break a genuinely run-on line, and only at a label that starts a
        # sentence or follows one, never mid-phrase.
        rebuilt, last = [], 0
        for match in _INLINE_LABEL.finditer(line):
            before = line[:match.start()].rstrip()
            if match.start() == 0 or not before or before[-1] in ".;。；" or before.endswith("  "):
                rebuilt.append(line[last:match.start()])
                last = match.start()
        rebuilt.append(line[last:])
        lines.extend(part.strip() for part in rebuilt if part.strip())
    return "\n".join(lines)


def _heading_kind(line: str) -> Optional[BlockKind]:
    """Classify a line as a section heading, or return None."""
    stripped = line.strip()
    if not stripped or len(stripped) > 70:
        return None
    # Markdown and all-caps headings are explicit; both still have to name a
    # section we recognise, so "PYTHON, SQL, DOCKER" is not read as a heading.
    candidate = re.sub(r"^#{1,6}\s*", "", stripped)
    candidate = re.sub(r"^\*+|\*+$|^_+|_+$", "", candidate).strip()
    candidate = candidate.rstrip(":：").strip()
    if not candidate or not any(char.isalpha() for char in candidate):
        return None
    for kind, pattern in _SECTIONS:
        if pattern.match(candidate):
            return kind  # type: ignore[return-value]
    return None


def _months_between(text: str) -> Optional[int]:
    """Duration in months for the first date range on a line, if one parses."""
    match = _DATE_RANGE.search(text)
    if not match:
        return None

    def point(value: str) -> Optional[tuple[int, int]]:
        value = value.strip().rstrip(".").casefold()
        if re.fullmatch(r"present|current|now|今", value):
            return None
        year = re.search(r"\d{4}", value)
        if not year:
            return None
        month = 1
        if name := re.search(r"[a-z]{3,}", value):
            key = name.group()[:3]
            month = _MONTHS.get(key) or _SEASON_MONTH.get(
                next((season for season in _SEASON_MONTH if value.startswith(season)), ""), 1)
        elif numeric := re.match(r"(\d{1,2})[/-]\d{4}", value):
            month = min(12, max(1, int(numeric.group(1))))
        return int(year.group()), month

    parts = re.split(r"\s*(?:[-–—]|to|至|~)\s*", match.group(), maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    start, end = point(parts[0]), point(parts[1])
    if start is None:
        return None
    if end is None:  # "Present" — not a duration we can claim, only a start.
        return None
    span = (end[0] - start[0]) * 12 + (end[1] - start[1]) + 1
    return span if 0 < span <= 600 else None


def _entries(lines: list[tuple[int, str]], kind: BlockKind) -> Iterator[list[tuple[int, str]]]:
    """Split a section's lines into one group per role/project.

    A new entry starts at a non-bullet line carrying a date range, which is how
    essentially every resume separates positions. Sections without dates stay a
    single entry rather than being guessed apart.
    """
    if kind not in {"experience", "project", "publication", "activity", "education"}:
        yield lines
        return
    current: list[tuple[int, str]] = []
    for offset, line in lines:
        starts_entry = bool(_DATE_RANGE.search(line)) and not _BULLET.match(line)
        if starts_entry and current and any(text.strip() for _, text in current):
            yield current
            current = []
        current.append((offset, line))
    if current:
        yield current


def prepare(text: str) -> tuple[str, list[Block]]:
    """Return (normalised text, blocks). Block offsets index the returned text.

    Layout normalisation rewrites the string — it gives inline section labels
    their own line — so handing back only the blocks would leave their offsets
    pointing into a document the caller no longer has. Every consumer that
    resolves an offset (skill attribution, quote verification) must use the
    text returned here.
    """
    text = _normalise_layout(clean_unicode(text))
    blocks: list[Block] = []
    offset = 0
    lines: list[tuple[int, str]] = []
    for line in text.splitlines(keepends=True):
        lines.append((offset, line.rstrip("\r\n")))
        offset += len(line)

    sections: list[tuple[BlockKind, str, list[tuple[int, str]]]] = []
    # Everything before the first recognised heading is the contact header.
    current_kind: BlockKind = "header"
    current_heading = ""
    current: list[tuple[int, str]] = []
    for position, line in lines:
        kind = _heading_kind(line)
        if kind is not None:
            sections.append((current_kind, current_heading, current))
            current_kind, current_heading, current = kind, line.strip().rstrip(":："), []
            continue
        inline = _split_inline_label(line)
        if inline is not None:
            kind, heading, remainder = inline
            sections.append((current_kind, current_heading, current))
            # Keep the offset pointing at the content, so quotes still resolve
            # to a real span of the original text.
            current_kind, current_heading = kind, heading
            current = [(position + len(line) - len(remainder), remainder)] if remainder else []
            continue
        current.append((position, line))
    sections.append((current_kind, current_heading, current))

    counter = 0
    for kind, heading, section_lines in sections:
        if not any(line.strip() for _, line in section_lines):
            continue
        for group in _entries(section_lines, kind):
            body = [(position, line) for position, line in group if line.strip()]
            if not body:
                continue
            start = body[0][0]
            end = body[-1][0] + len(body[-1][1])
            counter += 1
            block_text = text[start:end]
            blocks.append(Block(
                id=f"b{counter}", kind=kind, heading=heading, text=block_text,
                start=start, end=end,
                bullets=[collapse(_BULLET.sub("", line)) for _, line in body if _BULLET.match(line)],
                months=_months_between(block_text) if kind in EVIDENCE_KINDS else None,
            ))
    return text, blocks


def segment(text: str) -> list[Block]:
    """Blocks only, for callers that do not resolve offsets themselves."""
    return prepare(text)[1]


def block_containing(blocks: list[Block], quote: str) -> Optional[Block]:
    """Find the block a verbatim quote came from.

    Evidence classification is decided here, in code, from where the text
    actually lives — never from what a model says about its own citation.
    """
    needle = collapse(quote)
    if len(needle) < 8:
        return None
    for block in blocks:
        if needle in collapse(block.text):
            return block
    return None


def evidence_text(blocks: list[Block]) -> str:
    """Only the parts of a resume that demonstrate work, for retrieval."""
    return "\n".join(block.text for block in blocks if block.is_evidence)
