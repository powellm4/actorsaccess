# tests/test_role_selector.py
"""Tests for the role selector's parsing and selection logic.

These tests mock the Anthropic API to test parsing without making real API calls.
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

from src.role_selector import (
    _is_transient_error,
    analyze_submission_requirements,
    select_best_roles,
)


SAMPLE_ROLES = [
    {"role_name": "Jake", "role_type": "Lead", "age_range": "25-30", "gender": "Male", "description": "Confident protagonist"},
    {"role_name": "Officer Dan", "role_type": "Supporting", "age_range": "40-50", "gender": "Male", "description": "Grizzled veteran cop"},
    {"role_name": "Tommy", "role_type": "Lead", "age_range": "22-28", "gender": "Male", "description": "Charming con artist"},
]


def _make_mock_anthropic(response_text: str):
    """Create a mock anthropic module with a preset response."""
    mock_module = MagicMock()
    mock_client = MagicMock()
    mock_module.Anthropic.return_value = mock_client
    mock_response = MagicMock()
    mock_content = MagicMock()
    mock_content.text = response_text
    mock_response.content = [mock_content]
    mock_client.messages.create.return_value = mock_response
    return mock_module, mock_client


def _make_mock_anthropic_error():
    """Create a mock anthropic module that raises on create."""
    mock_module = MagicMock()
    mock_client = MagicMock()
    mock_module.Anthropic.return_value = mock_client
    mock_client.messages.create.side_effect = Exception("API timeout")
    return mock_module, mock_client


def test_single_role_fit_check():
    """Single candidate should pass AI fitness check."""
    mock_module, mock_client = _make_mock_anthropic("FIT - Good physical and type match")
    roles = [SAMPLE_ROLES[0]]
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Test Project")
    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert rejections == {}


def test_single_role_skip():
    """Single candidate that fails fitness check should be skipped."""
    mock_module, mock_client = _make_mock_anthropic("SKIP - Requires heavyset build")
    roles = [SAMPLE_ROLES[0]]
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Test Project")
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
    mock_module, _ = _make_mock_anthropic(preamble_response)
    roles = [SAMPLE_ROLES[0]]
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Test Project")
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
    mock_module, _ = _make_mock_anthropic(preamble_response)
    roles = [SAMPLE_ROLES[0]]
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Test Project")
    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert rejections == {}


def test_single_role_no_api_key_returns_directly():
    """Single candidate without API key should return without check."""
    roles = [SAMPLE_ROLES[0]]
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        selected, rejections = select_best_roles(roles, "Test Project")
    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert rejections == {}


def test_no_api_key_falls_back_to_first():
    """Missing API key should return first role."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")
    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert "no API key" in selected[0][1]
    assert rejections == {}


def test_single_selection_parsed():
    """AI selecting one role should parse correctly."""
    mock_anthropic, _ = _make_mock_anthropic(
        "SELECTED: 1 - Best physical and type match\nREJECTED: 2 - Age range too high for actor\nREJECTED: 3 - Similar to role 1 but less prominent"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert "Best physical and type match" in selected[0][1]
    assert "Officer Dan" in rejections
    assert "Tommy" in rejections


def test_double_selection_parsed():
    """AI selecting two roles should parse both."""
    mock_anthropic, _ = _make_mock_anthropic(
        "SELECTED: 1 - Great leading man fit\nSELECTED: 3 - Also a strong charming type\nREJECTED: 2 - Age range 40-50 is too old"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 2
    assert selected[0][0]["role_name"] == "Jake"
    assert selected[1][0]["role_name"] == "Tommy"
    assert "Officer Dan" in rejections


def test_skip_returns_empty_selected():
    """AI returning SKIP should return empty selected list."""
    mock_anthropic, _ = _make_mock_anthropic(
        "SKIP - All roles require age 40+ which doesn't match actor profile"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 0
    assert len(rejections) == 3


def test_malformed_response_falls_back_to_first():
    """Unparseable AI response should fall back to first role."""
    mock_anthropic, _ = _make_mock_anthropic(
        "I think role 1 is the best choice because..."
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert "unparseable" in selected[0][1].lower()
    assert len(rejections) == 2


def test_api_failure_falls_back_to_first():
    """API exception should fall back to first role."""
    mock_anthropic, _ = _make_mock_anthropic_error()
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert "API timeout" in selected[0][1]


def test_three_selections_all_kept():
    """AI returning 3 SELECTED lines should keep all 3."""
    mock_anthropic, _ = _make_mock_anthropic(
        "SELECTED: 1 - Great fit\nSELECTED: 2 - Also good\nSELECTED: 3 - Third pick"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
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
    mock_module, _ = _make_mock_anthropic(f"SKIP - {_BLADES_REASON}")
    role = {"role_name": "Ryan", "description": _BLADES_DESC}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "BLADES OF LOVE")
    assert len(selected) == 1, f"expected override → selected, got rejections={rejections}"
    assert selected[0][0]["role_name"] == "Ryan"
    assert "clears threshold" in selected[0][1].lower()
    assert rejections == {}


def test_no_override_when_pay_below_threshold():
    """Local-hire SKIP should stand if pay is too low for the location tier."""
    mock_module, _ = _make_mock_anthropic("SKIP - Atlanta local hire only")
    role = {
        "role_name": "Bob",
        "description": "Local hire only to Atlanta, GA. $100/day for 1 day.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Cheap Atlanta")
    assert selected == []
    assert "Bob" in rejections


def test_no_override_for_legitimate_skip_reason():
    """SKIP for height/skills/etc. must never be overridden, even with travel-pay."""
    mock_module, _ = _make_mock_anthropic("SKIP - Requires 6'4\" minimum, actor is 6'0\"")
    role = {
        "role_name": "Tall",
        "description": "Must be 6'4\"+. Local hire only to LA. $5000 total.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Tall People Project")
    assert selected == []
    assert "Tall" in rejections


def test_no_override_in_unpaid_mode():
    """Unpaid mode has no pay-threshold rules; the override must not fire."""
    mock_module, _ = _make_mock_anthropic(f"SKIP - {_BLADES_REASON}")
    role = {"role_name": "Ryan", "description": _BLADES_DESC}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
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
    mock_module, _ = _make_mock_anthropic(response)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
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
    mock_anthropic, _ = _make_mock_anthropic("ACTION: SUBMIT")
    role = {"role_name": "Jake", "description": "Male lead, 25-30."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "SUBMIT"
    assert result["note"] is None
    assert result["needs_input_reason"] is None


def test_analyze_submit_with_note():
    """Answerable requirements should return SUBMIT_WITH_NOTE."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: I'm LA local with reliable transportation and open availability."
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(SAMPLE_ROLE, "Test Project")
    assert result["action"] == "SUBMIT_WITH_NOTE"
    assert "LA local" in result["note"]
    assert result["needs_input_reason"] is None


def test_analyze_needs_input():
    """Unanswerable requirements should return NEEDS_INPUT."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: NEEDS_INPUT\nREASON: Casting requires SAG-AFTRA number"
    )
    role = {"role_name": "Jake", "description": "Must provide SAG-AFTRA number."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "NEEDS_INPUT"
    assert result["note"] is None
    assert "SAG-AFTRA" in result["needs_input_reason"]


def test_analyze_needs_input_local_hire_overridden():
    """NEEDS_INPUT that's really "actor isn't local to [city]" should flip to SUBMIT
    when travel pay clears the threshold. The actor works as a local hire anywhere."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: NEEDS_INPUT\n"
        "REASON: Role requires being local to San Francisco; actor is based in Los Angeles and cannot claim SF local status."
    )
    role = {
        "role_name": "Young Rider",
        "description": "Confident e-bike rider in San Francisco. Must be local to SF. PAY: $1,500",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "E-Bike Brand Photoshoot", mode="paid")
    assert result["action"] == "SUBMIT"
    assert result["needs_input_reason"] is None


def test_analyze_needs_input_local_hire_not_overridden_when_pay_too_low():
    """Local-hire NEEDS_INPUT should still be flagged when travel pay doesn't clear the threshold."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: NEEDS_INPUT\n"
        "REASON: Must be local to New York; actor cannot claim NY local status."
    )
    role = {
        "role_name": "Rider",
        "description": "New York shoot. Must be local to NYC. PAY: $200 flat",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "NYC Shoot", mode="paid")
    assert result["action"] == "NEEDS_INPUT"
    assert "local" in result["needs_input_reason"].lower()


def test_analyze_api_failure_raises():
    """API failure should raise to stop the run."""
    mock_anthropic, _ = _make_mock_anthropic_error()
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            with pytest.raises(Exception, match="API timeout"):
                analyze_submission_requirements(SAMPLE_ROLE, "Test Project")


def test_analyze_no_api_key_raises():
    """Missing API key should raise to stop the run."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY not set"):
            analyze_submission_requirements(SAMPLE_ROLE, "Test Project")


def test_analyze_empty_description_defaults_to_submit():
    """Empty description should return SUBMIT without API call."""
    role = {"role_name": "Jake", "description": ""}
    result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "SUBMIT"


# --- confirmed_dates tests ---


def test_analyze_with_confirmed_dates():
    """When confirmed_dates is provided, AI should include dates in note."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: I have full availability April 5-12, 2026. LA local with reliable transportation."
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(
                {"role_name": "Jake", "description": "Must note availability April 5-12."},
                "Test Project",
                confirmed_dates="2026-04-05 to 2026-04-12",
            )
    assert result["action"] == "SUBMIT_WITH_NOTE"
    assert "April 5-12" in result["note"]


def test_analyze_without_confirmed_dates():
    """Without confirmed_dates, AI should still generate notes for other requirements."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: I'm LA local with reliable transportation."
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
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


# --- _is_transient_error classification ---


def _exc_with_status(status: int, msg: str = "boom"):
    e = RuntimeError(msg)
    e.status_code = status
    return e


def test_transient_classifies_overload_and_rate_limit():
    assert _is_transient_error(_exc_with_status(429))
    assert _is_transient_error(_exc_with_status(500))
    assert _is_transient_error(_exc_with_status(529))


def test_transient_classifies_402_payment_required():
    assert _is_transient_error(_exc_with_status(402))


def test_transient_classifies_anthropic_credit_balance_400():
    # Real Anthropic billing error: HTTP 400 invalid_request_error whose
    # message points the operator at Plans & Billing. Must NOT be persisted
    # as a permanent rejection.
    msg = (
        "Error code: 400 - {'type': 'error', 'error': {'type': "
        "'invalid_request_error', 'message': 'Your credit balance is too low "
        "to access the Anthropic API. Please go to Plans & Billing to upgrade "
        "or purchase credits.'}}"
    )
    assert _is_transient_error(_exc_with_status(400, msg))


def test_transient_classifies_quota_or_billing_mentions():
    assert _is_transient_error(RuntimeError("you have exceeded your quota"))
    assert _is_transient_error(RuntimeError("insufficient_quota"))


def test_non_transient_for_generic_400():
    # A plain malformed-prompt 400 (no billing/quota signal) should NOT be
    # treated as transient — we want it persisted so we don't keep retrying.
    assert not _is_transient_error(_exc_with_status(400, "bad prompt format"))
