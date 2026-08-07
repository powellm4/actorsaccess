# tests/test_browser.py
"""Tests for pure string-processing helpers in src/browser.py.

The Playwright-driven scraping itself isn't unit tested (no live page to
scrape against), but _clean_scraped_description() is plain string logic and
can be tested directly.
"""
from src.browser import _clean_scraped_description


# --- leading "]" artifact (casting-suggestion #122) ---


def test_strips_leading_bracket():
    """Every AA role description scraped so far starts with a stray "]"
    immediately after the role link — e.g. the July 28, 2026 digest's
    "]Man; 18 to 28 years old; ... LEAD" (Falling In Line — Josh)."""
    text = "]Man; 18 to 28 years old; all ethnicities. A top cadet at Whitmore Academy."
    assert _clean_scraped_description(text) == "Man; 18 to 28 years old; all ethnicities. A top cadet at Whitmore Academy."


def test_no_leading_bracket_is_a_no_op():
    """Descriptions without the artifact (e.g. non-AA platforms) pass through unchanged."""
    text = "Fit, attractive but approachable, commercial type"
    assert _clean_scraped_description(text) == text


# --- trailing "Match" badge / stray bracket artifact ---


def test_strips_trailing_match_and_bracket():
    """Roles the site flags as a fit pick up a trailing "Match" badge + bracket
    from a sibling UI element the DOM walk doesn't stop at — e.g. the July 28,
    2026 digest's "THE METHOD" — ENZO description, which ended with
    "...are not local. \\n\\nMatch\\n[" before the AI's "Reason:" line."""
    text = (
        "Talent must be local to the New York/New Jersey area. "
        "Please do not submit if you are not local. \n\nMatch\n["
    )
    cleaned = _clean_scraped_description(text)
    assert cleaned == "Talent must be local to the New York/New Jersey area. Please do not submit if you are not local."
    assert "Match" not in cleaned
    assert "[" not in cleaned


def test_strips_trailing_bracket_without_match_label():
    """Some fit roles pick up just the trailing bracket without the "Match" text
    (e.g. the July 28, 2026 digest's POP STAR CLONE — POP STAR #2 description,
    which ended directly on "...No simulated sex scenes scheduled. [")."""
    text = "No simulated sex scenes scheduled. ["
    cleaned = _clean_scraped_description(text)
    assert cleaned == "No simulated sex scenes scheduled."
    assert "[" not in cleaned


def test_legitimate_trailing_content_is_untouched():
    """A description that happens to end mid-sentence without the artifact
    should not be mangled."""
    text = "Must be comfortable being shirtless on camera."
    assert _clean_scraped_description(text) == text


def test_empty_and_none_pass_through():
    assert _clean_scraped_description("") == ""
    assert _clean_scraped_description(None) is None


def test_both_artifacts_together():
    text = "]Man; 20 to 60 years old; White. Jimmy Fallon look-a-like. \n\nMatch\n["
    cleaned = _clean_scraped_description(text)
    assert cleaned == "Man; 20 to 60 years old; White. Jimmy Fallon look-a-like."
