"""Resume segmentation: the step that decides what counts as evidence."""
import pytest

from sww.resume.segment import EVIDENCE_KINDS, block_containing, prepare, segment

STRUCTURED = """Jane Doe
jane@example.com

EDUCATION
University of Waterloo, BASc Computer Science    Sep 2023 - Apr 2028

WORK EXPERIENCE
Software Engineering Intern, Acme Corp           May 2025 - Aug 2025
- Built an event ingestion service with Python asyncio.

Data Analyst Intern, Beta Inc                    Jan 2024 - Apr 2024
- Wrote SQL pipelines.

PROJECTS
Telemetry Normalizer                             Sep 2024 - Dec 2024
- Aggregates daily metrics using SQL.

TECHNICAL SKILLS
Languages: Python, SQL, Rust
"""

RUN_ON = ("Education: second-year Computer Science undergraduate. "
          "Experience: built an event ingestion service with Python asyncio. "
          "Skills: Python, SQL, Rust, Go.")


def kinds(text):
    return [block.kind for block in segment(text)]


def test_sections_and_entries_are_separated():
    blocks = segment(STRUCTURED)
    assert kinds(STRUCTURED) == ["header", "education", "experience", "experience", "project", "skills"]
    # Each role is its own block, so one internship cannot lend its duration to another.
    assert [block.months for block in blocks if block.kind == "experience"] == [4, 4]


def test_skills_list_is_never_evidence():
    blocks = segment(STRUCTURED)
    skills = next(block for block in blocks if block.kind == "skills")
    experience = next(block for block in blocks if block.kind == "experience")
    assert not skills.is_evidence
    assert experience.is_evidence
    assert block_containing(blocks, "Languages: Python, SQL, Rust").kind == "skills"
    assert block_containing(blocks, "event ingestion service with Python").kind == "experience"


def test_run_on_resume_still_splits_on_inline_labels():
    """PDF extraction often yields one long line; a single blob would leave the
    matcher with nothing but a skills list to compare against."""
    assert kinds(RUN_ON) == ["education", "experience", "skills"]
    assert any(block.is_evidence for block in segment(RUN_ON))


def test_block_offsets_index_the_text_that_is_returned():
    text, blocks = prepare(RUN_ON)
    for block in blocks:
        assert text[block.start:block.end] == block.text


@pytest.mark.parametrize("text,expected", [
    ("EXPERIENCE\nIntern, X   May 2024 - Aug 2024\n- Did work.", 4),
    ("EXPERIENCE\nIntern, X   Jan 2024 - Dec 2024\n- Did work.", 12),
    ("EXPERIENCE\nIntern, X   May 2024 - Present\n- Did work.", None),
    ("EXPERIENCE\nIntern, X   2024\n- Did work.", None),
])
def test_duration_is_only_claimed_when_both_ends_are_stated(text, expected):
    blocks = [block for block in segment(text) if block.kind in EVIDENCE_KINDS]
    assert blocks and blocks[0].months == expected


def test_all_caps_skill_line_is_not_mistaken_for_a_heading():
    text = "EXPERIENCE\nIntern, X   May 2024 - Aug 2024\nPYTHON, SQL, DOCKER\n"
    assert kinds(text) == ["experience"]
