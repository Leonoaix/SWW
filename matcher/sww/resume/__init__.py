"""Resume ingestion: bytes -> text -> labelled blocks -> structured profile."""

from ..config import (
    MAX_PDF_PAGES, MAX_RESUME_BYTES, MAX_RESUME_CHARACTERS,
)
from .documents import Document, extract_resume
from .profile import (
    Availability, CandidateProfile, EducationItem, ExperienceItem, ResumeAnalysis,
    SkillClaim, build_profile, heuristic_profile, prompt_payload, refresh_recency,
    verify_quote,
)
from .segment import Block, block_containing, evidence_text, prepare, segment
from .skills import SKILL_ALIASES, demonstrated_skills, extract_skills, find_mentions

# Historical alias: PDF callers used this name before DOCX and Markdown
# shared the same limit.
MAX_PDF_BYTES = MAX_RESUME_BYTES

__all__ = [
    "MAX_PDF_BYTES", "MAX_PDF_PAGES", "MAX_RESUME_BYTES", "MAX_RESUME_CHARACTERS",
    "Availability", "Block", "CandidateProfile", "Document", "EducationItem", "ExperienceItem",
    "ResumeAnalysis", "SKILL_ALIASES", "SkillClaim", "block_containing", "build_profile",
    "demonstrated_skills", "evidence_text", "extract_resume", "extract_skills", "find_mentions",
    "heuristic_profile", "prepare", "prompt_payload", "refresh_recency", "segment",
    "verify_quote",
]
