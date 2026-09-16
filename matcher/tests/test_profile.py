"""Structured resume decomposition, with and without a model."""
import pytest

from conftest import FakeClient, NOW, RESUME
from sww.llm import DeepSeekError, Extractor
from sww.resume import ResumeAnalysis, build_profile, prompt_payload, refresh_recency, verify_quote
from sww.resume.skills import demonstrated_skills
from sww.resume.segment import segment


async def test_local_split_produces_roles_durations_and_availability():
    analysis = await build_profile(RESUME, now=NOW)
    profile = analysis.profile
    assert profile.source == "heuristic"
    assert [item.kind for item in profile.experiences] == ["work", "project"]
    assert profile.experiences[0].title == "Software Engineering Intern"
    assert profile.experiences[0].organization == "Example Corp"
    assert profile.work_experience_months == 4
    assert profile.total_experience_months == 8
    assert profile.availability.months == [4]


async def test_skills_are_split_by_where_they_are_written():
    """The distinction the old pipeline re-derived with a regex at scoring time."""
    analysis = await build_profile(RESUME, now=NOW)
    assert "Python" in analysis.profile.demonstrated_skills
    assert "SQL" in analysis.profile.demonstrated_skills
    # Named in the skills list, used in no described work.
    assert set(analysis.profile.listed_skills) >= {"Rust", "Go"}
    assert "Rust" not in analysis.profile.demonstrated_skills


def test_demonstrated_and_listed_are_disjoint():
    demonstrated, listed = demonstrated_skills(RESUME, segment(RESUME))
    assert not set(demonstrated) & set(listed)


async def test_quote_verification_reports_both_existence_and_evidence():
    analysis = await build_profile(RESUME, now=NOW)
    assert verify_quote(analysis, "asynchronous Python services with durable queues") == (True, True)
    assert verify_quote(analysis, "Languages: Python, SQL, Rust, Go") == (True, False)
    assert verify_quote(analysis, "Ten years of production experience") == (False, False)


async def test_prompt_payload_carries_verbatim_evidence_but_not_the_skills_list():
    analysis = await build_profile(RESUME, now=NOW)
    payload = prompt_payload(analysis)
    joined = "\n".join(item["text"] for item in payload["experiences"])
    assert "durable queues" in joined
    # A quote can only be taken from evidence, so the list cannot be cited.
    assert "Languages: Python, SQL, Rust, Go" not in joined
    assert payload["listed_only_skills"]


async def test_model_split_must_cite_a_real_block(store):
    """An invented experience cannot survive verification."""
    fabricated = {"headline": "", "domains": [], "education": [], "availability": {},
                  "experiences": [{"block_id": "b99", "anchor": "Led a team of forty engineers",
                                   "kind": "work", "title": "Director", "organization": "Nowhere",
                                   "start": "", "end": "", "months": 24, "summary": "",
                                   "highlights": [], "technologies": []}]}
    client = FakeClient()
    client.responses["match"] = fabricated
    extractor = Extractor(client, store, "test")
    with pytest.raises(DeepSeekError, match="anchor|核对"):
        await build_profile(RESUME, extractor)


async def test_model_split_cannot_override_a_stated_duration(store):
    """Dates are arithmetic, not judgement: the resume's own range wins."""
    blocks = segment(RESUME)
    experience = next(block for block in blocks if block.kind == "experience")
    client = FakeClient()
    client.responses["match"] = {
        "headline": "后端方向二年级", "domains": ["backend"], "education": [],
        "availability": {"months": [4]},
        "experiences": [{"block_id": experience.id, "anchor": "Software Engineering Intern",
                         "kind": "work", "title": "Software Engineering Intern",
                         "organization": "Example Corp", "start": "May 2025", "end": "Aug 2025",
                         "months": 24, "summary": "构建异步服务", "highlights": [],
                         "technologies": ["Python"]}]}
    analysis = await build_profile(RESUME, Extractor(client, store, "test"))
    assert analysis.profile.source == "model"
    assert analysis.profile.experiences[0].months == 4


async def test_oversized_resume_is_refused_rather_than_truncated(store):
    with pytest.raises(DeepSeekError, match="40,000|上限"):
        await build_profile("x " * 30_000, Extractor(FakeClient(), store, "test"))


async def test_a_stored_analysis_is_dated_from_today_not_from_when_it_was_written():
    """The analysis is kept across restarts, and `months_ago` is a distance
    from today rather than a fact about the resume. Recency is a scored
    dimension, so reading one back unchanged would rank as though no time had
    passed since the upload."""
    from datetime import date

    analysis = await build_profile(RESUME, now=NOW)
    written = [item.months_ago for item in analysis.profile.experiences]
    assert written and all(months is not None for months in written)

    stored = ResumeAnalysis.model_validate_json(analysis.model_dump_json())
    assert [item.months_ago for item in stored.profile.experiences] == written
    assert stored.text == analysis.text

    a_year_later = refresh_recency(stored, now=date(NOW.year + 1, NOW.month, NOW.day))
    assert [item.months_ago for item in a_year_later.profile.experiences] == [m + 12 for m in written]
    # Newest first is derived from these distances and is load-bearing: the
    # cross-encoder query and the model payload are both length-capped.
    months = [item.months_ago for item in a_year_later.profile.experiences]
    assert months == sorted(months)
    assert all(item.recency < 1.0 for item in a_year_later.profile.experiences)


@pytest.mark.parametrize("headline,title,organization", [
    # "Employer, Location — Role": splitting on the first comma read this
    # backwards and filed the employer as the job title.
    ("Noah Lab, Waterloo, ON — Research Assistant Sep 2025 – Apr 2026",
     "Research Assistant", "Noah Lab, Waterloo, ON"),
    ("Acme Telecom, Chengdu, China — Engineering Intern (Co-op) May 2024 – Aug 2024",
     "Engineering Intern (Co-op)", "Acme Telecom, Chengdu, China"),
    # "Role, Employer" keeps working: the side naming a job decides, not the order.
    ("Backend Intern, Acme Corp May 2024 – Aug 2024", "Backend Intern", "Acme Corp"),
    ("Software Engineer — Acme Corp", "Software Engineer", "Acme Corp"),
    # Neither side names a role: nothing is invented, nothing is swapped.
    ("Telemetry Normalizer Oct 2025 – Dec 2025", "Telemetry Normalizer", ""),
    ("Research Assistant, University of Waterloo 09/2024 - 12/2024",
     "Research Assistant", "University of Waterloo"),
])
def test_an_entry_headline_is_split_by_which_side_names_a_job(headline, title, organization):
    from sww.resume.profile import _split_entry

    assert _split_entry(headline) == (title, organization)


def test_dates_are_stripped_from_an_entry_headline():
    """Every backslash in this pattern was doubled, so its character classes
    were read as literal backslashes and it matched nothing at all. Dates rode
    into the employer field on every resume."""
    from sww.resume.profile import _DATE_IN_LINE

    for headline in ("Acme Corp May 2024 – Aug 2024", "Acme Corp 09/2024 - 12/2024",
                     "Acme Corp Sep 2025 – Present"):
        assert _DATE_IN_LINE.sub("", headline).strip(" ,|-–—") == "Acme Corp"


async def test_an_undated_entry_comes_back_as_null_and_is_read_as_unstated(store):
    """The prompt tells the model to use null for anything the resume does not
    state, and it applies that to text fields too: an undated project returns
    `"start": null`. Declaring those `str` made the commonest resume shape a
    schema failure, and the whole extraction fell back to the local split."""
    blocks = segment(RESUME)
    experience = next(block for block in blocks if block.kind == "experience")
    client = FakeClient()
    client.responses["match"] = {
        "headline": None, "domains": None, "education": [], "availability": None,
        "experiences": [{"block_id": experience.id, "anchor": "Software Engineering Intern",
                         "kind": "project", "title": "Telemetry Normalizer",
                         "organization": None, "start": None, "end": None, "months": None,
                         "summary": "规范化遥测数据", "highlights": None, "technologies": None}]}
    analysis = await build_profile(RESUME, Extractor(client, store, "test"))
    assert analysis.profile.source == "model"
    item = analysis.profile.experiences[0]
    assert (item.start, item.end, item.organization) == ("", "", "")
    assert (item.technologies, item.highlights) == ([], [])
    assert analysis.profile.headline == "" and analysis.profile.domains == []
    # A null availability is not an empty one: the resume's own statement fills it.
    assert analysis.profile.availability.months == [4]
