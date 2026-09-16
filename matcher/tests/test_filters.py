"""Pre-scoring rules. Both engines call exactly these, so they cannot diverge."""
from copy import deepcopy
from datetime import datetime

import pytest

from conftest import job
from sww.match.filters import (
    TORONTO, deadline_passed, eligible, required_term_months, shortest_term_months,
    term_exclusion, term_limit,
)
from sww.resume.profile import Availability, CandidateProfile

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=TORONTO)


def profile(months):
    return CandidateProfile(availability=Availability(months=months))


@pytest.mark.parametrize("value,expected", [
    ("2026-09-14", True), ("2026-09-15", False), ("2026-09-16", False),
    ("Sep 14, 2026 11:59 PM", True), ("September 16, 2026 11:59 PM", False),
    ("2026-09-15 11:59 PM", False),
])
def test_date_only_deadlines_last_until_the_end_of_that_toronto_day(value, expected):
    assert deadline_passed(value, NOW)[0] is expected


@pytest.mark.parametrize("value,fragment", [
    ("", "No application deadline"), ("whenever", "could not be parsed"),
])
def test_unparseable_deadlines_warn_instead_of_excluding(value, fragment):
    passed, warning = deadline_passed(value, NOW)
    assert passed is False and fragment in warning


@pytest.mark.parametrize("text,months,mandatory", [
    ("An uninterrupted eight-month placement is mandatory.", {8}, True),
    ("A 8 month work term is required.", {8}, True),
    ("An eight-month placement is preferred, but four-month placements are equally eligible.", {8, 4}, False),
    ("Four-month placement.", {4}, False),
    ("We build things.", set(), False),
])
def test_term_length_is_read_from_either_digits_or_words(text, months, mandatory):
    found, is_mandatory, _ = required_term_months(job("t", description=text))
    assert found == months and is_mandatory is mandatory


@pytest.mark.parametrize("description,shortest", [
    ("An uninterrupted eight-month placement is mandatory; four-month placements are not accepted.", 8),
    # Accepts four, so a four-month student can take it: the minimum is what counts,
    # not whether eight months is mentioned.
    ("An eight-month placement is preferred, but four-month placements are equally eligible.", 4),
    ("Four-month placement.", 4),
    ("This is a 12 month position.", 12),
    ("We build reliable services.", None),
])
def test_shortest_acceptable_term_is_what_decides_reachability(description, shortest):
    assert shortest_term_months(job("t", description=description))[0] == shortest


def test_structured_work_term_field_is_read_before_prose():
    posting = job("t", description="We build things.", metadata={"Work Term Duration": "8 month"})
    assert shortest_term_months(posting)[0] == 8


@pytest.mark.parametrize("preferences,profile_months,expected", [
    ({}, [4], 4),                       # falls back to the resume's own availability
    ({}, [], None),                     # nothing stated anywhere: no filtering
    ({"max_term_months": 8}, [4], 8),   # an explicit preference wins
    ({"max_term_months": 0}, [4], None),  # 0 turns the filter off
    ({}, [4, 8], 8),                    # the longest the resume offers
])
def test_term_limit_prefers_an_explicit_setting_over_the_resume(preferences, profile_months, expected):
    assert term_limit(preferences, profile(profile_months)) == expected


def test_a_posting_out_of_reach_on_term_length_is_excluded_with_its_own_wording():
    posting = job("long", description="An uninterrupted eight-month placement is mandatory.")
    reason = term_exclusion(posting, 4)
    assert "8" in reason and "4" in reason and "mandatory" in reason


@pytest.mark.parametrize("description", [
    "An eight-month placement is preferred, but four-month placements are equally eligible.",
    "Four-month placement.",
    "We build reliable services.",
])
def test_reachable_or_unstated_terms_are_never_excluded(description):
    assert term_exclusion(job("t", description=description), 4) == ""


def test_term_filtering_excludes_rather_than_flags():
    postings = [job("long", description="An eight-month placement is mandatory."),
                job("short", description="Four-month placement."),
                job("silent")]
    rankable, excluded = eligible(postings, {}, profile=profile([4]), now=NOW)
    assert {item["id"] for item in rankable} == {"short", "silent"}
    assert [item["id"] for item in excluded] == ["long"]


def test_a_resume_without_stated_availability_filters_nothing_by_term():
    postings = [job("long", description="An eight-month placement is mandatory.")]
    assert len(eligible(postings, {}, profile=profile([]), now=NOW)[0]) == 1


def test_closed_expired_keyword_and_duplicate_postings_are_excluded():
    postings = [
        job("closed", is_open=False), job("expired", deadline="2020-01-01"),
        job("keyword", title="Senior Developer"), job("open"), job("open"),
    ]
    rankable, excluded = eligible(postings, {"exclude_keywords": ["Senior"]}, now=NOW)
    assert [item["id"] for item in rankable] == ["open"]
    assert {item["reason"] for item in excluded} == {
        "职位已关闭。", "申请截止日期已过。",
        "命中排除关键词：Senior", "重复职位。"}


def test_duplicate_merge_keeps_the_richer_copy_and_the_closed_status():
    postings = [job("1", description="short", is_open=False),
                job("1", description="a considerably longer description of the work")]
    rankable, _ = eligible(postings, {}, now=NOW)
    assert rankable == []  # merged copy inherits the closed flag


def test_inputs_are_never_mutated():
    postings = [job("1"), job("2", is_open=False)]
    before = deepcopy(postings)
    eligible(postings, {"exclude_keywords": ["nothing"]}, profile=profile([4]), now=NOW)
    assert postings == before
