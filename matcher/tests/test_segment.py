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


# A structured resume the way a PDF actually arrives: sections named with
# qualifiers, undated projects, wrapped bullet text, and a skills section whose
# first line carries a sub-label that also names a section.
REAL_WORLD = """Jane Doe
jane@example.com

EDUCATION
University of Waterloo, BASc Computer Engineering Sep 2023 - Apr 2028

RESEARCH EXPERIENCE
Noah Lab, Waterloo, ON - Research Assistant Sep 2025 - Apr 2026
• Studied reward stability across 1.5B-7B models, running baselines and
 analysing variance across scales.
• Deployed Slurm scheduling over 64 GPUs.

INDUSTRY EXPERIENCE
Acme Telecom, Chengdu, China - Engineering Intern (Co-op) May 2024 - Aug 2024
• Built a retrieval service with FastAPI and Milvus, serving 50 users
 with sub-200ms latency.

PERSONAL PROJECTS
Knowledge Graph Explorer Oct 2025 - Dec 2025
• Combined LLM query parsing with graph traversal.
Contract Risk Analyzer University of Waterloo
• Finetuned a 7B model with LoRA, reaching F1 0.82 on held-out
 contract clauses.
AI Study Assistant University of Waterloo
• Integrated RAG with a vector store for course material.

TECHNICAL SKILLS
Research: Reinforcement Learning, Knowledge Graphs, RAG
Programming: Python, PyTorch, SQL
"""


def test_a_qualified_experience_heading_is_still_an_experience_heading():
    """"RESEARCH EXPERIENCE" is as common as "WORK EXPERIENCE", and enumerating
    the qualifiers was never going to keep up. Bare "Research" is still a
    publications heading, which is a different section entirely."""
    blocks = segment(REAL_WORLD)
    headings = {block.heading: block.kind for block in blocks if block.heading}
    assert headings["RESEARCH EXPERIENCE"] == "experience"
    assert headings["INDUSTRY EXPERIENCE"] == "experience"
    assert segment("Research\nSome paper, 2024\n")[-1].kind == "publication"


def test_an_inline_label_does_not_hijack_a_section_that_has_a_real_heading():
    """Inline labels are how a run-on extraction expresses sections. Reading
    them inside a structured document filed a technical-skills list under
    "Research:" as research experience — turning claims into demonstrated work."""
    blocks = segment(REAL_WORLD)
    skills = [block for block in blocks if block.kind == "skills"]
    assert [block.heading for block in skills] == ["TECHNICAL SKILLS"]
    assert "Python, PyTorch, SQL" in skills[0].text
    assert not skills[0].is_evidence
    assert not any(block.heading == "Research" for block in blocks)


def test_undated_projects_are_separate_entries_and_wrapped_bullets_are_not():
    """Projects and publications routinely carry no dates, and used to collapse
    into one entry holding the whole section. The risk in splitting without a
    date is wrapped bullet text, which keeps its indent and starts lower-case."""
    projects = [block for block in segment(REAL_WORLD) if block.kind == "project"]
    assert len(projects) == 3
    assert [block.text.splitlines()[0] for block in projects] == [
        "Knowledge Graph Explorer Oct 2025 - Dec 2025",
        "Contract Risk Analyzer University of Waterloo",
        "AI Study Assistant University of Waterloo",
    ]
    # The continuation of a bullet must never become an entry of its own.
    assert not any(block.text.startswith("contract clauses") for block in segment(REAL_WORLD))
