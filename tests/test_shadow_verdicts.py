# tests/test_shadow_verdicts.py
"""Unit tests for verdict extractors in src/shadow.py."""

from src.shadow import VERDICT_EXTRACTORS


# ---------------------------------------------------------------------------
# select_best_roles
# ---------------------------------------------------------------------------

def test_select_best_roles_basic():
    extract = VERDICT_EXTRACTORS["select_best_roles"]
    text = "SELECTED: 1 - foo\nREJECTED: 2 - bar\nSELECTED: 3 - baz"
    assert extract(text) == "SELECTED:{1,3}|REJECTED:{2}"


def test_select_best_roles_all_rejected():
    extract = VERDICT_EXTRACTORS["select_best_roles"]
    text = "REJECTED: 1 - too tall\nREJECTED: 2 - too short"
    assert extract(text) == "SELECTED:{}|REJECTED:{1,2}"


def test_select_best_roles_skip():
    extract = VERDICT_EXTRACTORS["select_best_roles"]
    assert extract("SKIP") == "SKIP"
    assert extract("SKIP - none of these fit") == "SKIP"


def test_select_best_roles_skip_with_leading_whitespace():
    """Real responses sometimes have leading whitespace before SKIP."""
    extract = VERDICT_EXTRACTORS["select_best_roles"]
    assert extract("  SKIP - reason") == "SKIP"


def test_select_best_roles_unparseable_returns_none():
    extract = VERDICT_EXTRACTORS["select_best_roles"]
    assert extract("I'm not sure about this one.") is None


def test_select_best_roles_sorts_and_dedups():
    extract = VERDICT_EXTRACTORS["select_best_roles"]
    text = "SELECTED: 3 - a\nSELECTED: 1 - b\nSELECTED: 3 - dup\nREJECTED: 2 - c"
    assert extract(text) == "SELECTED:{1,3}|REJECTED:{2}"


def test_select_best_roles_none_input():
    extract = VERDICT_EXTRACTORS["select_best_roles"]
    assert extract(None) is None


# ---------------------------------------------------------------------------
# single_fit
# ---------------------------------------------------------------------------

def test_single_fit_fit():
    extract = VERDICT_EXTRACTORS["single_fit"]
    assert extract("FIT - good match") == "FIT"


def test_single_fit_skip():
    extract = VERDICT_EXTRACTORS["single_fit"]
    assert extract("SKIP - wrong build") == "SKIP"


def test_single_fit_with_leading_markdown():
    extract = VERDICT_EXTRACTORS["single_fit"]
    assert extract("**FIT** - good match") == "FIT"
    assert extract("- SKIP - bad") == "SKIP"


def test_single_fit_with_preamble_line():
    """Sonnet occasionally writes preamble before the verdict — we scan all lines."""
    extract = VERDICT_EXTRACTORS["single_fit"]
    text = "Looking at this role...\nFIT - athletic match"
    assert extract(text) == "FIT"


def test_single_fit_no_verdict():
    extract = VERDICT_EXTRACTORS["single_fit"]
    assert extract("I think maybe yes") is None


# ---------------------------------------------------------------------------
# partial_availability
# ---------------------------------------------------------------------------

def test_partial_availability_proceed():
    extract = VERDICT_EXTRACTORS["partial_availability"]
    assert extract("PROCEED - 4 of 5 days available") == "PROCEED"


def test_partial_availability_skip():
    extract = VERDICT_EXTRACTORS["partial_availability"]
    assert extract("SKIP - too many conflicts") == "SKIP"


def test_partial_availability_leading_whitespace():
    extract = VERDICT_EXTRACTORS["partial_availability"]
    assert extract("   PROCEED - works") == "PROCEED"


# ---------------------------------------------------------------------------
# prescreen (pass-through)
# ---------------------------------------------------------------------------

def test_prescreen_pass_through_answered():
    extract = VERDICT_EXTRACTORS["prescreen"]
    assert extract("answered") == "answered"


def test_prescreen_pass_through_needs_input():
    extract = VERDICT_EXTRACTORS["prescreen"]
    assert extract("needs_input") == "needs_input"


def test_prescreen_json_payload_heuristic():
    extract = VERDICT_EXTRACTORS["prescreen"]
    text = '{"answers": [{"question_id": 1, "selected_answer_id": 2}]}'
    assert extract(text) == "answered"


def test_prescreen_needs_input_in_text():
    extract = VERDICT_EXTRACTORS["prescreen"]
    text = "Some question is needs_input because it's open-ended."
    assert extract(text) == "needs_input"


# ---------------------------------------------------------------------------
# submission_requirements
# ---------------------------------------------------------------------------

def test_submission_requirements_submit():
    extract = VERDICT_EXTRACTORS["submission_requirements"]
    assert extract("ACTION: SUBMIT") == "SUBMIT"


def test_submission_requirements_submit_with_note():
    extract = VERDICT_EXTRACTORS["submission_requirements"]
    text = "ACTION: SUBMIT_WITH_NOTE\nNOTE: I am available 4/12-4/25."
    assert extract(text) == "SUBMIT_WITH_NOTE"


def test_submission_requirements_needs_input():
    extract = VERDICT_EXTRACTORS["submission_requirements"]
    assert extract("ACTION: NEEDS_INPUT\nREASON: need demo reel") == "NEEDS_INPUT"


def test_submission_requirements_first_action_wins():
    """If response has more than one ACTION line, take the first."""
    extract = VERDICT_EXTRACTORS["submission_requirements"]
    text = "ACTION: SUBMIT\nACTION: NEEDS_INPUT"
    assert extract(text) == "SUBMIT"


def test_submission_requirements_unknown_action():
    extract = VERDICT_EXTRACTORS["submission_requirements"]
    assert extract("ACTION: REJECT") is None


def test_submission_requirements_no_action():
    extract = VERDICT_EXTRACTORS["submission_requirements"]
    assert extract("I think we should submit.") is None


# ---------------------------------------------------------------------------
# cover_letter (always None)
# ---------------------------------------------------------------------------

def test_cover_letter_returns_none():
    extract = VERDICT_EXTRACTORS["cover_letter"]
    assert extract("Anything at all goes here.") is None
    assert extract("") is None
    assert extract(None) is None


# ---------------------------------------------------------------------------
# Sanity: all 6 call sites present
# ---------------------------------------------------------------------------

def test_all_call_sites_have_extractors():
    expected = {
        "select_best_roles",
        "single_fit",
        "partial_availability",
        "prescreen",
        "submission_requirements",
        "cover_letter",
    }
    assert set(VERDICT_EXTRACTORS.keys()) == expected
