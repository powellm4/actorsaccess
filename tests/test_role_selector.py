# tests/test_role_selector.py
"""Tests for the role selector's parsing and selection logic.

These tests mock the Claude CLI subprocess to test parsing without invoking
the real CLI.
"""
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.role_selector import select_best_roles, analyze_submission_requirements


SAMPLE_ROLES = [
    {"role_name": "Jake", "role_type": "Lead", "age_range": "25-30", "gender": "Male", "description": "Confident protagonist"},
    {"role_name": "Officer Dan", "role_type": "Supporting", "age_range": "40-50", "gender": "Male", "description": "Grizzled veteran cop"},
    {"role_name": "Tommy", "role_type": "Lead", "age_range": "22-28", "gender": "Male", "description": "Charming con artist"},
]


@contextmanager
def _mock_claude_cli(response_text: str):
    """Patch the Claude CLI subprocess to return a preset response.

    Also makes shutil.which("claude") return a truthy path so the
    availability check passes.
    """
    fake_proc = SimpleNamespace(stdout=response_text, stderr="", returncode=0)
    with patch("src.role_selector.shutil.which", return_value="/usr/bin/claude"), \
         patch("src.shadow.subprocess.run", return_value=fake_proc) as mock_run:
        yield mock_run


@contextmanager
def _mock_claude_cli_error(exc: Exception):
    """Patch the Claude CLI subprocess to raise on call."""
    with patch("src.role_selector.shutil.which", return_value="/usr/bin/claude"), \
         patch("src.shadow.subprocess.run", side_effect=exc) as mock_run:
        yield mock_run


@contextmanager
def _mock_no_claude_cli():
    """Patch shutil.which to report the Claude CLI is missing."""
    with patch("src.role_selector.shutil.which", return_value=None):
        yield


def test_single_role_fit_check():
    """Single candidate should pass AI fitness check."""
    with _mock_claude_cli("FIT - Good physical and type match"):
        selected, rejections = select_best_roles([SAMPLE_ROLES[0]], "Test Project")
    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert rejections == {}


def test_single_role_skip():
    """Single candidate that fails fitness check should be skipped."""
    with _mock_claude_cli("SKIP - Requires heavyset build"):
        selected, rejections = select_best_roles([SAMPLE_ROLES[0]], "Test Project")
    assert len(selected) == 0
    assert "Jake" in rejections


def test_single_role_skip_with_preamble_still_parsed():
    """Regression: Sonnet sometimes writes reasoning before the verdict line.
    The parser must scan all lines, not just the first, so the SKIP decision
    isn't lost behind preamble (and the role isn't silently flagged as
    'AI response unrecognized').
    """
    preamble_response = (
        "Looking at this role: **VON** — 29-49, all ethnicities, man, "
        "easygoing or uptight/serious, one day, non-union, $125/day, Denver, CO.\n\n"
        "**Checking hard disqualifiers:**\n"
        "- Age range: 29-49. Actor plays 17-29. Minimal overlap at the low end.\n"
        "- Location: Denver, CO — travel required, pay doesn't cover travel.\n\n"
        "SKIP - Age range 29-49 has no meaningful overlap with actor's 17-29"
    )
    with _mock_claude_cli(preamble_response):
        selected, rejections = select_best_roles([SAMPLE_ROLES[0]], "Test Project")
    assert len(selected) == 0
    assert "Jake" in rejections
    assert "29-49" in rejections["Jake"] or "Age range" in rejections["Jake"]
    # Must NOT fall into the "AI response unrecognized" branch
    assert "unrecognized" not in rejections["Jake"].lower()


def test_single_role_fit_with_preamble_still_parsed():
    """Same as above but for a FIT verdict after preamble reasoning."""
    preamble_response = (
        "Evaluating this role: The character is a leading man, 24 years old, "
        "athletic build, LA-based.\n\n"
        "FIT - Age and type match; LA local so no travel concern"
    )
    with _mock_claude_cli(preamble_response):
        selected, rejections = select_best_roles([SAMPLE_ROLES[0]], "Test Project")
    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert rejections == {}


def test_no_cli_available_skips_all_roles():
    """Without the Claude CLI on PATH, every role is rejected as 'skipped'."""
    with _mock_no_claude_cli():
        selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")
    assert selected == []
    assert set(rejections.keys()) == {"Jake", "Officer Dan", "Tommy"}
    for reason in rejections.values():
        assert "Claude CLI" in reason


def test_single_selection_parsed():
    """AI selecting one role should parse correctly."""
    with _mock_claude_cli(
        "SELECTED: 1 - Best physical and type match\n"
        "REJECTED: 2 - Age range too high for actor\n"
        "REJECTED: 3 - Similar to role 1 but less prominent"
    ):
        selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert "Best physical and type match" in selected[0][1]
    assert "Officer Dan" in rejections
    assert "Tommy" in rejections


def test_double_selection_parsed():
    """AI selecting two roles should parse both."""
    with _mock_claude_cli(
        "SELECTED: 1 - Great leading man fit\n"
        "SELECTED: 3 - Also a strong charming type\n"
        "REJECTED: 2 - Age range 40-50 is too old"
    ):
        selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 2
    assert selected[0][0]["role_name"] == "Jake"
    assert selected[1][0]["role_name"] == "Tommy"
    assert "Officer Dan" in rejections


def test_skip_returns_empty_selected():
    """AI returning SKIP should return empty selected list."""
    with _mock_claude_cli(
        "SKIP - All roles require age 40+ which doesn't match actor profile"
    ):
        selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 0
    assert len(rejections) == 3


def test_malformed_response_skips_all_roles():
    """Unparseable AI response should reject all roles (safer than fabricating selections)."""
    with _mock_claude_cli("I think role 1 is the best choice because..."):
        selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert selected == []
    assert set(rejections.keys()) == {"Jake", "Officer Dan", "Tommy"}


def test_api_failure_skips_all_roles():
    """CLI subprocess failure should reject all roles."""
    with _mock_claude_cli_error(Exception("CLI timeout")):
        selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert selected == []
    assert set(rejections.keys()) == {"Jake", "Officer Dan", "Tommy"}
    for reason in rejections.values():
        assert "CLI timeout" in reason


def test_three_selections_all_kept():
    """AI returning 3 SELECTED lines should keep all 3."""
    with _mock_claude_cli(
        "SELECTED: 1 - Great fit\nSELECTED: 2 - Also good\nSELECTED: 3 - Third pick"
    ):
        selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 3
    assert rejections == {}


# --- local-hire override tests ---
#
# Regression: the AI was rejecting BLADES OF LOVE — RYAN (Charlotte, NC,
# $4,200 total) by acknowledging the pay cleared the fly-to threshold and
# then rationalizing around the rule via "actor cannot present as a
# Charlotte local hire." The override should catch this exact pattern.

_BLADES_REASON = (
    "Charlotte, NC local hire only; actor is based in Los Angeles, and at $700/day × 6 days "
    "= $4,200 total, the pay exceeds the fly-to threshold of $1,000 — however, the casting "
    "explicitly requires talent local to Charlotte, NC as a hard local hire condition with "
    "no indication of travel/relocation reimbursement, and the actor cannot genuinely "
    "present as a Charlotte local hire."
)
_BLADES_DESC = (
    "20 to 25 years old; man. Hockey star turned figure skater. "
    "Shoots for 6 days. Location: Charlotte, NC. Rate of Pay: $700/day. "
    "Casting talent local to CHARLOTTE, NC ONLY"
)


def test_override_local_hire_when_pay_clears_threshold_single_role():
    """AI SKIP citing 'local hire' should be overridden when pay clears threshold."""
    role = {"role_name": "Ryan", "description": _BLADES_DESC}
    with _mock_claude_cli(f"SKIP - {_BLADES_REASON}"):
        selected, rejections = select_best_roles([role], "BLADES OF LOVE")
    assert len(selected) == 1, f"expected override → selected, got rejections={rejections}"
    assert selected[0][0]["role_name"] == "Ryan"
    assert "clears threshold" in selected[0][1].lower()
    assert rejections == {}


def test_no_override_when_pay_below_threshold():
    """Local-hire SKIP should stand if pay is too low for the location tier."""
    role = {
        "role_name": "Bob",
        "description": "Local hire only to Atlanta, GA. $100/day for 1 day.",
    }
    with _mock_claude_cli("SKIP - Atlanta local hire only"):
        selected, rejections = select_best_roles([role], "Cheap Atlanta")
    assert selected == []
    assert "Bob" in rejections


def test_no_override_for_legitimate_skip_reason():
    """SKIP for height/skills/etc. must never be overridden, even with travel-pay."""
    role = {
        "role_name": "Tall",
        "description": "Must be 6'4\"+. Local hire only to LA. $5000 total.",
    }
    with _mock_claude_cli("SKIP - Requires 6'4\" minimum, actor is 6'0\""):
        selected, rejections = select_best_roles([role], "Tall People Project")
    assert selected == []
    assert "Tall" in rejections


def test_no_override_in_unpaid_mode():
    """Unpaid mode has no pay-threshold rules; the override must not fire."""
    role = {"role_name": "Ryan", "description": _BLADES_DESC}
    with _mock_claude_cli(f"SKIP - {_BLADES_REASON}"):
        selected, rejections = select_best_roles([role], "BLADES", mode="unpaid")
    assert selected == []
    assert "Ryan" in rejections


def test_override_in_multi_role_path():
    """Multi-role REJECTED with local-hire reason should be moved to SELECTED when pay clears."""
    roles = [
        {
            "role_name": "Ava",
            "role_type": "Lead",
            "description": "Female figure skater. Charlotte, NC. $700/day × 6 days.",
        },
        {
            "role_name": "Ryan",
            "role_type": "Lead",
            "description": _BLADES_DESC,
        },
    ]
    response = (
        "REJECTED: 1 - Female-only, actor is male\n"
        f"REJECTED: 2 - {_BLADES_REASON}"
    )
    with _mock_claude_cli(response):
        selected, rejections = select_best_roles(roles, "BLADES OF LOVE")
    selected_names = [s[0]["role_name"] for s in selected]
    assert "Ryan" in selected_names, f"Ryan should have been overridden; got {selected_names} / {rejections}"
    assert "Ava" in rejections  # legit female-only rejection should stand


# --- analyze_submission_requirements tests ---

SAMPLE_ROLE = {
    "role_name": "Jake",
    "description": "Looking for a 25-30 male lead. Please include your availability and location in your submission notes.",
}


def test_analyze_submit_no_requirements():
    """No special requirements should return SUBMIT."""
    role = {"role_name": "Jake", "description": "Male lead, 25-30."}
    with _mock_claude_cli("ACTION: SUBMIT"):
        result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "SUBMIT"
    assert result["note"] is None
    assert result["needs_input_reason"] is None


def test_analyze_submit_with_note():
    """Answerable requirements should return SUBMIT_WITH_NOTE."""
    with _mock_claude_cli(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: I'm LA local with reliable transportation and open availability."
    ):
        result = analyze_submission_requirements(SAMPLE_ROLE, "Test Project")
    assert result["action"] == "SUBMIT_WITH_NOTE"
    assert "LA local" in result["note"]
    assert result["needs_input_reason"] is None


def test_analyze_needs_input():
    """Unanswerable requirements should return NEEDS_INPUT."""
    role = {"role_name": "Jake", "description": "Must provide SAG-AFTRA number."}
    with _mock_claude_cli("ACTION: NEEDS_INPUT\nREASON: Casting requires SAG-AFTRA number"):
        result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "NEEDS_INPUT"
    assert result["note"] is None
    assert "SAG-AFTRA" in result["needs_input_reason"]


def test_analyze_api_failure_raises():
    """CLI subprocess failure should raise to stop the run."""
    with _mock_claude_cli_error(Exception("CLI timeout")):
        with pytest.raises(Exception, match="CLI timeout"):
            analyze_submission_requirements(SAMPLE_ROLE, "Test Project")


def test_analyze_no_cli_raises():
    """Missing Claude CLI should raise to stop the run."""
    with _mock_no_claude_cli():
        with pytest.raises(RuntimeError, match="Claude CLI not on PATH"):
            analyze_submission_requirements(SAMPLE_ROLE, "Test Project")


def test_analyze_empty_description_defaults_to_submit():
    """Empty description should return SUBMIT without invoking the CLI."""
    role = {"role_name": "Jake", "description": ""}
    # CLI must still be reported present so the early "no CLI" guard doesn't fire,
    # but no subprocess call should be made.
    with patch("src.role_selector.shutil.which", return_value="/usr/bin/claude"), \
         patch("src.shadow.subprocess.run") as mock_run:
        result = analyze_submission_requirements(role, "Test Project")
        assert mock_run.call_count == 0
    assert result["action"] == "SUBMIT"


# --- confirmed_dates tests ---


def test_analyze_with_confirmed_dates():
    """When confirmed_dates is provided, AI should include dates in note."""
    with _mock_claude_cli(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: I have full availability April 5-12, 2026. LA local with reliable transportation."
    ):
        result = analyze_submission_requirements(
            {"role_name": "Jake", "description": "Must note availability April 5-12."},
            "Test Project",
            confirmed_dates="2026-04-05 to 2026-04-12",
        )
    assert result["action"] == "SUBMIT_WITH_NOTE"
    assert "April 5-12" in result["note"]


def test_analyze_without_confirmed_dates():
    """Without confirmed_dates, AI should still generate notes for other requirements."""
    with _mock_claude_cli(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: I'm LA local with reliable transportation."
    ):
        result = analyze_submission_requirements(
            {"role_name": "Jake", "description": "LA local hire only."},
            "Test Project",
        )
    assert result["action"] == "SUBMIT_WITH_NOTE"
    assert "LA local" in result["note"]


# --- parse_shoot_dates tests ---


def test_parse_shoot_dates_same_month():
    """Parse 'April 12-25, 2026' format."""
    from src.calendar_check import parse_shoot_dates
    result = parse_shoot_dates("Shoot Dates: April 12 - 25, 2026")
    assert result == ("2026-04-12", "2026-04-25")


def test_parse_shoot_dates_no_spaces():
    """Parse 'April 7-18, 2026' format (no space around dash)."""
    from src.calendar_check import parse_shoot_dates
    result = parse_shoot_dates("April 7-18, 2026")
    assert result == ("2026-04-07", "2026-04-18")


def test_parse_shoot_dates_cross_month():
    """Parse 'March 28 - April 5, 2026' format."""
    from src.calendar_check import parse_shoot_dates
    result = parse_shoot_dates("Shoot Dates: March 28 - April 5, 2026")
    assert result == ("2026-03-28", "2026-04-05")


def test_parse_shoot_dates_no_dates():
    """Return None when no dates found."""
    from src.calendar_check import parse_shoot_dates
    result = parse_shoot_dates("No dates mentioned here")
    assert result is None
