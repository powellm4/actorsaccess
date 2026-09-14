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
    _maybe_override_local_hire_skip,
    _unmet_gender_role_name_conflict,
    _validate_note,
    analyze_submission_requirements,
    check_travel_pay,
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


def test_single_role_real_skill_player_requirement_overrides_fit():
    """Regression for casting-suggestion (TENNIS PRO, July 14 2026 Paid digest):
    a description reading "REAL TENNIS PLAYER / ATHLETE" is a non-negotiable skill
    requirement, but the AI prompt's example list ("singing, musical instrument,
    specific martial art") doesn't call this phrasing out, so the AI rationalized
    past it ("casting does not explicitly exclude non-specialists") and returned
    FIT. The actor's profile lists volleyball, not tennis, so the programmatic
    backstop must demote this FIT to a rejection."""
    role = dict(SAMPLE_ROLES[0])
    role["description"] = (
        "REAL TENNIS PLAYER / ATHLETE - Tennis Pro at the country club teaching "
        "our member how to play!"
    )
    mock_module, _ = _make_mock_anthropic(
        "FIT - Athletic build fits; casting does not explicitly exclude non-specialists"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Test Project")
    assert len(selected) == 0
    assert "Jake" in rejections
    assert "tennis" in rejections["Jake"].lower()


def test_single_role_real_skill_player_requirement_not_triggered_when_actor_has_skill():
    """The actor's profile lists volleyball, so a "REAL VOLLEYBALL PLAYER" role must
    still be accepted — the backstop only demotes skills genuinely missing from the
    profile."""
    role = dict(SAMPLE_ROLES[0])
    role["description"] = "REAL VOLLEYBALL PLAYER wanted for a sports drink commercial."
    mock_module, _ = _make_mock_anthropic("FIT - Athletic build and volleyball background fit")
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Test Project")
    assert len(selected) == 1
    assert rejections == {}


def test_multi_role_real_skill_player_requirement_demotes_selected():
    """Same backstop, exercised through the multi-role SELECTED/REJECTED path."""
    roles = [dict(r) for r in SAMPLE_ROLES[:2]]
    roles[0]["description"] = (
        "REAL TENNIS PLAYER / ATHLETE - Tennis Pro at the country club."
    )
    response = (
        "SELECTED: 1 - Age and build fit, tennis not confirmed but role is open\n"
        "REJECTED: 2 - Requires heavyset build, actor is athletic"
    )
    mock_module, _ = _make_mock_anthropic(response)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Test Project")
    assert len(selected) == 0
    assert "Jake" in rejections
    assert "tennis" in rejections["Jake"].lower()


def test_single_role_self_correction_fit_to_skip_wins_on_final_verdict():
    """Regression for the BILT/SNYK bug (casting-suggestion #65/#75): the model builds
    an APPLY case, self-corrects mid-reasoning, and ends on SKIP — the code must trust
    the LAST verdict token, not the first."""
    response = (
        "FIT - $500/day pay clears the $1000 NYC fly-to threshold (single shoot day = "
        "$500 — wait, actually this does NOT clear $1000). SKIP - NYC fly-to location "
        "requires $1000 minimum, role pays only $500."
    )
    mock_module, _ = _make_mock_anthropic(response)
    roles = [SAMPLE_ROLES[0]]
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Test Project")
    assert selected == [], f"final SKIP conclusion must win; got selected={selected}"
    assert "Jake" in rejections
    assert "$500" in rejections["Jake"]


def test_single_role_self_correction_skip_to_fit_wins_on_final_verdict():
    """Regression for the 'blond actor' bug (casting-suggestion #67/#69): the model
    initially leans SKIP on a soft preference, re-evaluates, and concludes FIT — the
    code must trust the LAST verdict, not strand the role in the PASS bucket."""
    response = (
        "SKIP - role requires blond hair, actor has brown hair, could be a hard "
        "disqualifier. Re-evaluating: general hair color is a styling choice achievable "
        "through dyeing, not a disqualifier for regular acting roles. FIT - age range "
        "overlaps, hair color achievable through dyeing, no hard disqualifiers found."
    )
    mock_module, _ = _make_mock_anthropic(response)
    roles = [SAMPLE_ROLES[0]]
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Test Project")
    assert len(selected) == 1, f"final FIT conclusion must win; got rejections={rejections}"
    assert selected[0][0]["role_name"] == "Jake"
    assert rejections == {}


def test_multi_role_rejected_self_correction_to_fit_wins_on_final_verdict():
    """Regression for casting-suggestion #86: in the multi-role SELECTED/REJECTED
    path, a REJECTED reason that self-corrects and concludes FIT must be promoted
    to selected, not stranded verbatim in rejections (e.g. Polo Sporting Goods /
    J-Mo, July 4 2026 Paid digest — reasoning ends "FIT - Hispanic ethnicity
    qualifies, age overlaps, valid license... " yet the role was passed)."""
    response = (
        "SELECTED: 1 - Age and type match for athletic leading man\n"
        "SELECTED: 3 - Charming con artist type fits well\n"
        "REJECTED: 2 - Ethnicity requirement excludes actor at first glance — "
        "however, re-evaluating: the listed ethnicities do include the actor's. "
        "FIT - ethnicity qualifies, age overlaps, no hard disqualifiers"
    )
    mock_module, _ = _make_mock_anthropic(response)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")
    selected_names = {r["role_name"] for r, _ in selected}
    assert "Officer Dan" in selected_names, (
        f"final FIT conclusion must promote the role out of rejections; "
        f"selected={selected_names}, rejections={rejections}"
    )
    assert "Officer Dan" not in rejections


def test_multi_role_selected_self_correction_to_skip_wins_on_final_verdict():
    """Regression for the symmetric gap left open by #86 (its fix only covered
    REJECTED->selected, not SELECTED->rejected): a SELECTED reason that
    self-corrects and concludes with a DISQUALIFIER must be demoted to
    rejections (e.g. Eric Mason / 27 CLUB, July 6 2026 UNPAID digest —
    reasoning ends "DISQUALIFIER: requires electric bass guitar, a skill the
    actor does not have" yet the role was still applied to)."""
    response = (
        "SELECTED: 1 - Age and type match for athletic leading man\n"
        "SELECTED: 3 - requires electric bass guitar which the actor does not "
        "explicitly have — wait, actor plays guitar well but bass is not listed. "
        "DISQUALIFIER: requires electric bass guitar, a skill the actor does not have.\n"
        "REJECTED: 2 - Requires heavyset build, actor is athletic"
    )
    mock_module, _ = _make_mock_anthropic(response)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")
    selected_names = {r["role_name"] for r, _ in selected}
    assert "Tommy" not in selected_names, (
        f"final DISQUALIFIER conclusion must demote the role out of selected; "
        f"selected={selected_names}"
    )
    assert "Tommy" in rejections
    assert "Jake" in selected_names


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


def test_bare_skip_with_no_reason_does_not_store_uninformative_token():
    """Regression: 'PASSING THE BAR' digest evidence — the AI responded with
    a bare 'SKIP' line and no reason, and the rejection reason ended up being
    the literal string 'SKIP', which gives a human sanity-checking the digest
    zero information ('Reason (all roles): SKIP'). Any preceding reasoning in
    the response should be used instead, and the stored reason must never be
    the bare token itself."""
    mock_anthropic, _ = _make_mock_anthropic(
        "These roles don't fit the actor's profile well.\nSKIP"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 0
    assert len(rejections) == 3
    for reason in rejections.values():
        assert reason.strip().upper() != "SKIP"
        assert "don't fit the actor's profile" in reason


def test_skip_alone_on_first_line_captures_explanation_from_next_lines():
    """AI writing bare 'SKIP' on its own line, with the explanation on the
    following line(s) instead of after a dash, must not be reduced to the
    bare, uninformative word "SKIP" as the stored reason for every role.
    """
    mock_anthropic, _ = _make_mock_anthropic(
        "SKIP\n"
        "All roles are background/atmosphere crowd work for a bank commercial "
        "with no individual character identity."
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 0
    assert len(rejections) == 3
    for reason in rejections.values():
        assert reason != "SKIP"
        assert "background/atmosphere crowd work" in reason


def test_bare_skip_with_absolutely_no_context_gets_explicit_marker():
    """When the response is truly just 'SKIP' with nothing else, fall back to
    an explicit marker rather than the uninformative bare token."""
    mock_anthropic, _ = _make_mock_anthropic("SKIP")
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 0
    for reason in rejections.values():
        assert reason.strip().upper() != "SKIP"
        assert "no explanation" in reason.lower()


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


def test_no_override_when_reason_also_cites_non_waivable_disqualifier():
    """A real, independent disqualifier (e.g. athletic credential) alongside local-hire
    language must not be overridden just because travel pay clears the threshold —
    regression test for the Wahoo Fitness 'Elite Runner' bug (casting-suggestion #81)."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - Requires the actor to be a real runner logging at least 20 miles per week, "
        "ideally competing in races; actor is not a competitive runner and is not based in "
        "Oregon or Washington as required. $3000 for 1.5 days."
    )
    role = {
        "role_name": "Elite Runner",
        "description": "Real runner required. Oregon or Washington. $3000 + 20% for 1.5 days.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Wahoo Fitness")
    assert selected == [], f"non-waivable disqualifier must not be overridden; got {selected}"
    assert "Elite Runner" in rejections


def test_no_override_local_hire_when_bundled_age_claim_genuinely_disqualifies():
    """The AI sometimes bundles an unrelated age objection into a local-hire SKIP
    (e.g. 'Age range 35-55 has no overlap ... also requires local to Charlotte').
    Clearing the travel-pay threshold only resolves the local-hire clause — if the
    bundled age range genuinely doesn't overlap the actor's 17-30 range, the
    override must not fire just because pay clears."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - Age range 35-55 has no overlap with actor's 17-30 playable range; "
        "also requires local to Charlotte, NC with no travel reimbursement listed."
    )
    role = {
        "role_name": "Ryan",
        "description": (
            "35 to 55 years old; man. Hockey star turned figure skater. "
            "Shoots for 6 days. Location: Charlotte, NC. Rate of Pay: $700/day. "
            "Casting talent local to CHARLOTTE, NC ONLY"
        ),
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "BLADES OF LOVE")
    assert selected == [], f"genuine age mismatch must not be overridden; got {selected}"
    assert "Ryan" in rejections


def test_override_local_hire_when_bundled_age_claim_actually_overlaps():
    """Same bundled-reason shape as above, but the age range (30-50) does overlap
    the actor's 17-30 range at the 30-year boundary — the override should fire and
    the corrected reason should mention both the age overlap and the travel pay."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - Age range 30-50 has no overlap with actor's 17-30 playable range; "
        "also requires local to Charlotte, NC with no travel reimbursement listed."
    )
    role = {
        "role_name": "Ryan",
        "description": (
            "30 to 50 years old; man. Hockey star turned figure skater. "
            "Shoots for 6 days. Location: Charlotte, NC. Rate of Pay: $700/day. "
            "Casting talent local to CHARLOTTE, NC ONLY"
        ),
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "BLADES OF LOVE")
    assert len(selected) == 1, f"30-50 overlaps 17-30 at age 30; should be corrected. Got rejections={rejections}"
    reason = selected[0][1].lower()
    assert "overlaps" in reason
    assert "clears threshold" in reason
    assert rejections == {}


def test_no_override_local_hire_when_bundled_age_claim_unverifiable():
    """When the bundled age claim can't be turned into a numeric range (e.g. decade
    phrasing like '30's - 50's'), the override must not guess — a missed opportunity
    is cheaper than submitting to a role the age claim may genuinely disqualify."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - Age range 30s-50s has no overlap with actor's 17-30 playable range; "
        "also requires local to Charlotte, NC with no pay listed."
    )
    role = {
        "role_name": "Rep",
        "description": (
            "Males - 30's - 50's. To play a variety of roles like Store Rep, Sales Rep. "
            "Location: Charlotte, NC. Rate of Pay: $700/day for 6 days. "
            "Casting talent local to CHARLOTTE, NC ONLY"
        ),
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Salesforce Industrial")
    assert selected == [], f"unverifiable age claim must not be overridden; got {selected}"
    assert "Rep" in rejections


def test_override_uses_structured_pay_field_when_description_omits_rate():
    """The override must find pay in the role's structured field even when the free-text
    description doesn't spell out a rate (parity with the main.py call site fix)."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - New York local hire required; pay not specified in description."
    )
    role = {
        "role_name": "Model",
        "description": "New York local hire required. See breakdown for pay.",
        "pay": "$1500 flat",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Google Pay New York")
    assert len(selected) == 1, f"structured pay field should have cleared the threshold; got rejections={rejections}"
    assert selected[0][0]["role_name"] == "Model"


def test_no_override_when_structured_pay_field_is_too_low():
    """A low structured pay field must still block the override even though the
    free-text description has no parseable rate."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - New York local hire required; pay not specified in description."
    )
    role = {
        "role_name": "Model",
        "description": "New York local hire required. See breakdown for pay.",
        "pay": "$500 flat",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Google Pay New York")
    assert selected == []
    assert "Model" in rejections


def test_no_override_local_hire_when_no_pay_figure_exists_anywhere():
    """When a fly-to listing has no pay figure at all — not in the free-text
    description, not in a structured field, and no vague-pay phrasing either —
    check_travel_pay conservatively returns "clears" because it can't determine
    pay. That is not the same as pay actually clearing the threshold, so the
    local-hire override must not fire and fabricate a "clears threshold" claim.
    Regression for the NIGHT AT THE OASIS / Birmingham, AL casting-suggestion."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - Birmingham, AL local hire required; fly-to location with no pay "
        "mentioned, cannot confirm total pay meets the $1,000 fly-to threshold."
    )
    role = {
        "role_name": "Cow Worker",
        "description": (
            "Works at a cow farm. Day player. Must be Birmingham, Alabama local hire. "
            "Location: Birmingham, Alabama. No email pitches, please."
        ),
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Night at the Oasis")
    assert selected == [], f"no pay figure exists anywhere; override must not fabricate a clearance; got {selected}"
    assert "Cow Worker" in rejections


# --- age-overlap arithmetic backstop (casting-suggestion #70) ---


def test_override_age_overlap_when_ai_miscalculates_no_overlap_single_role():
    """A SKIP claiming 'no age overlap' for a range that DOES overlap the actor's
    17-30 playable range (28-38 has a 28-30 window) must be corrected."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - Age range 28-38 has no overlap with actor's 17-30 playable range; "
        "also blond/dark blond hair preferred."
    )
    role = {
        "role_name": "Dramatic Role",
        "description": "Lead dramatic role, 28-38 years old, blond or dark blond hair preferred.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Golden Era Project")
    assert len(selected) == 1, f"28-38 overlaps 17-30 at 28-30; should be corrected. Got rejections={rejections}"
    assert "overlaps" in selected[0][1].lower()


def test_no_override_age_overlap_when_range_genuinely_disqualifies():
    """A SKIP claiming 'no age overlap' for a range that truly doesn't overlap
    (31-40 vs. 17-30) must stand."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - Age range 31-40 has no overlap with actor's 17-30 playable range."
    )
    role = {"role_name": "Older Role", "description": "Character is 31 to 40 years old."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Test Project")
    assert selected == []
    assert "Older Role" in rejections


def test_no_override_age_overlap_for_unrelated_skip_reason():
    """A SKIP for an unrelated reason (no 'no overlap' + 'age' phrasing) must never
    trigger the age-overlap backstop, even if the description has a numeric range."""
    mock_module, _ = _make_mock_anthropic(
        "SKIP - Requires 6'4\" minimum height, actor is 6'0\"."
    )
    role = {"role_name": "Tall Role", "description": "25 to 35 years old, must be 6'4\"+."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Test Project")
    assert selected == []
    assert "Tall Role" in rejections


def test_no_override_age_overlap_when_bundled_with_solo_duo_disqualifier():
    """A SKIP that bundles a miscalculated age claim with a genuine 'requires a
    real duo, actor cannot submit solo' disqualifier must not be overridden —
    fixing the age math doesn't make a solo submission into a real pair.

    Modeled on the July 11, 2026 digest: AMAZON LEO — REAL DAD & DAUGHTER DUOS
    (father early-late 30s, daughter 4-6 years) was force-applied solo after
    the age-overlap backstop fired on a bogus merged range extracted from the
    two unrelated age bands, ignoring the AI's actual "cannot submit solo for
    a paired family role" disqualifier.
    """
    mock_module, _ = _make_mock_anthropic(
        "SKIP - Requires a real dad & daughter duo; actor cannot submit solo for "
        "a paired family role, and the father age range (early-late 30s) has no "
        "age overlap with actor's 17-30 playable range."
    )
    role = {
        "role_name": "REAL DAD & DAUGHTER DUOS",
        "description": "Father to be early-late 30s & daughter to be 4-6 years.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "AMAZON LEO NON UNION PRINT & VIDEO")
    assert selected == [], (
        f"Bundled 'cannot submit solo' disqualifier must block the override; got selected={selected}"
    )
    assert "REAL DAD & DAUGHTER DUOS" in rejections


def test_override_age_overlap_in_multi_role_path():
    """Multi-role REJECTED with a miscalculated age-overlap reason should move to
    SELECTED, mirroring the local-hire override's multi-role handling."""
    roles = [
        {"role_name": "Ava", "role_type": "Lead", "description": "Female role, 25-30."},
        {"role_name": "Russ", "role_type": "Lead", "description": "28 to 38 years old, athletic."},
    ]
    response = (
        "REJECTED: 1 - Female-only, actor is male\n"
        "REJECTED: 2 - Age range 28-38 has no overlap with actor's 17-30 playable range"
    )
    mock_module, _ = _make_mock_anthropic(response)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Golden Era Project")
    selected_names = [s[0]["role_name"] for s in selected]
    assert "Russ" in selected_names, f"Russ should have been overridden; got {selected_names} / {rejections}"
    assert "Ava" in rejections


# --- hard required-skill guard (casting-suggestion #73) ---


def test_fit_demoted_when_necessary_skill_is_missing_single_role():
    """A role explicitly marking a skill 'NECESSARY TO HAVE' that the actor doesn't
    have must be rejected even if the AI concludes FIT overall."""
    mock_module, _ = _make_mock_anthropic(
        "FIT - Strong type match, athletic build fits, comedic timing suits the role."
    )
    role = {
        "role_name": "Tyler Fletcher",
        "description": "Beach-set thriller. NECESSARY TO HAVE SWIMMING EXPERIENCE. Standout swimmer preferred.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "WET HOT BOYS")
    assert selected == [], f"missing hard-required skill must reject; got {selected}"
    assert "swimming" in rejections["Tyler Fletcher"].lower()


def test_fit_kept_when_necessary_skill_is_in_profile():
    """A 'NECESSARY TO HAVE' skill the actor's profile does list must not be rejected."""
    mock_module, _ = _make_mock_anthropic(
        "FIT - Strong type match; singing ability matches the musical number."
    )
    role = {
        "role_name": "Lead Singer",
        "description": "Musical short. NECESSARY TO HAVE SINGING experience.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Test Project")
    assert len(selected) == 1, f"actor can sing per profile; should not be rejected. Got rejections={rejections}"


def test_fit_demoted_when_must_be_able_to_swim_is_missing():
    """'Must be able to swim.' (no 'have'/'necessary' wording) must be caught too —
    this is the phrasing shape the original 'NECESSARY TO HAVE' regex missed."""
    mock_module, _ = _make_mock_anthropic(
        "FIT - Confident, charming lead type; swimming ability not confirmed but no hard disqualifier present."
    )
    role = {
        "role_name": "Lead Male",
        "description": "College age. Must be able to swim. The lead discovers a creature in the pool.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "The Deep End")
    assert selected == [], f"missing hard-required swimming skill must reject; got {selected}"
    assert "swim" in rejections["Lead Male"].lower()


def test_fit_kept_when_must_be_able_to_travel_is_not_a_skill_check():
    """Generic 'must be able to' logistics language (travel, attend, commit) is not
    a skill requirement and must not be treated as one."""
    mock_module, _ = _make_mock_anthropic(
        "FIT - Strong type match for the role."
    )
    role = {
        "role_name": "Traveler",
        "description": "Must be able to travel to set and commit to the full shoot schedule.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Test Project")
    assert len(selected) == 1, f"non-skill 'must be able to' phrasing must not reject; got rejections={rejections}"


def test_selected_demoted_when_necessary_skill_missing_multi_role_path():
    """Same guard applied to the multi-role SELECTED/REJECTED path."""
    roles = [
        {"role_name": "Angus", "role_type": "Supporting", "description": "NECESSARY TO HAVE SWIMMING EXPERIENCE. Beach thriller."},
        {"role_name": "Beckett", "role_type": "Supporting", "description": "Office drama, no special skills required."},
    ]
    response = (
        "SELECTED: 1 - Athletic build and general fitness make him plausible\n"
        "SELECTED: 2 - Strong type match for the office setting"
    )
    mock_module, _ = _make_mock_anthropic(response)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "WET HOT BOYS")
    selected_names = [s[0]["role_name"] for s in selected]
    assert "Angus" not in selected_names, f"missing swimming requirement must demote Angus; got {selected_names}"
    assert "Beckett" in selected_names
    assert "swimming" in rejections["Angus"].lower()


def test_no_skill_guard_when_requirement_is_a_soft_preference():
    """Ordinary (non-'necessary'/'must have') skill mentions must not trigger the guard."""
    mock_module, _ = _make_mock_anthropic(
        "FIT - Swimming skills a plus but not required; strong type match otherwise."
    )
    role = {"role_name": "Beach Extra", "description": "Swimming skills a plus for this beach commercial."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles([role], "Test Project")
    assert len(selected) == 1, f"soft preference must not trigger the hard-skill guard; got rejections={rejections}"


# --- check_travel_pay: flights/lodging covered waive the threshold ---

_LOVE_IS_IN_THE_AIR_DESC = (
    "Man; 25 to 35 years old. Matt's cousin and the object of Jessie's affection. "
    "Rate of Pay: $375/day. Flight, hotel, and off days $74/working days $30 per diem "
    "is provided. Location: Corpus Christi, TX & San Antonio, TX"
)


def test_travel_pay_waived_when_flight_and_hotel_provided():
    """The reported LOVE IS IN THE AIR notice should pass — flight + hotel covered."""
    ok, reason, _ = check_travel_pay("LOVE IS IN THE AIR", _LOVE_IS_IN_THE_AIR_DESC)
    assert ok is True, f"expected pass, got rejection: {reason}"
    assert reason is None


def test_travel_pay_waived_when_only_airfare_provided():
    """Flight coverage alone is enough to waive the fly-to threshold."""
    ok, reason, _ = check_travel_pay(
        "Indie Feature",
        "Lead role. Airfare provided. $200/day for 1 day. Shoots in Austin, TX.",
    )
    assert ok is True, f"expected pass, got rejection: {reason}"
    assert reason is None


def test_travel_pay_waived_when_only_hotel_provided():
    """Lodging coverage alone is enough to waive the fly-to threshold."""
    ok, reason, _ = check_travel_pay(
        "NY Short",
        "Supporting role. Hotel provided. $150/day for 1 day. Shoots in New York.",
    )
    assert ok is True, f"expected pass, got rejection: {reason}"
    assert reason is None


def test_travel_pay_not_waived_when_coverage_negated():
    """'No hotel or travel provided' must NOT trigger a waiver — low pay still rejects."""
    ok, reason, _ = check_travel_pay(
        "Cheap TX Gig",
        "Background-ish role. No hotel or travel provided. $100 total. Shoots in Dallas, TX.",
    )
    assert ok is False
    assert reason and "too low" in reason.lower()


def test_travel_pay_still_rejects_low_pay_without_coverage():
    """Regression guard: a plain low-pay fly-to role with no coverage language still rejects."""
    ok, reason, _ = check_travel_pay(
        "Low Pay NY",
        "Lead role. $50/day for 1 day. Shoots in New York.",
    )
    assert ok is False
    assert reason and "fly-to" in reason.lower()


# --- check_travel_pay: explicit Location: field beats an incidental LA mention ---
# See casting-suggestion evidence: COMPLICIT / JOSH GREENE, July 9 2026 (Paid) digest.
# The shoot is an unpaid Chicago local hire, but the project notes' boilerplate
# ("...premiered his latest film at Dances With Films in Los Angeles") mentioned
# Los Angeles once in an unrelated director bio. Because the LA-tier keyword scan
# ran over the whole notes blob, it matched "los angeles" before the notes' own
# explicit "Location: Chicago" field was ever consulted, and LA tier waives the
# pay threshold unconditionally — silently approving a $0-pay fly-to role.

_COMPLICIT_NOTES = (
    "COMPLICIT Feature Film SAG-AFTRA Micro Budget Agreement Executive Producer: "
    "Marissa Lichwick Shoot Dates: August 3 - 18, 2026 Rate of Pay: No pay. Copy, "
    "Credit, MealsLocation: Chicago DEADLINE: JULY 11, 2026 CHICAGO LOCAL HIRE. "
    "This is a SAG-AFTRA MICRO BUDGET PROJECT AGREEMENT. Our co-director recently "
    "premiered his latest film at Dances With Films in Los Angeles. We're "
    "assembling a passionate, collaborative cast and crew."
)


def test_travel_pay_explicit_location_field_beats_incidental_la_mention():
    """An unrelated 'Los Angeles' mention in bio/credits text must not override
    the notes' own explicit non-LA Location: field."""
    ok, reason, _ = check_travel_pay(
        "COMPLICIT", "Man; 20 to 27 years old. Supporting role.", _COMPLICIT_NOTES,
    )
    assert ok is False, "explicit 'Location: Chicago' should win over incidental 'Los Angeles' bio text"
    assert reason and "chicago" in reason.lower() and "$0" in reason


def test_travel_pay_la_location_field_still_waives_threshold():
    """Regression guard: a real, explicitly-stated LA shoot must still waive the threshold."""
    ok, reason, _ = check_travel_pay(
        "LA Short",
        "Background-ish role. $20 total.",
        "Location: Los Angeles, CA DEADLINE: JULY 10, 2026",
    )
    assert ok is True, f"expected LA location field to waive threshold, got rejection: {reason}"
    assert reason is None


def test_travel_pay_no_location_field_falls_back_to_full_text_scan():
    """Regression guard: without an explicit Location: field, the existing
    whole-text keyword scan still applies (no Location: field to prefer)."""
    ok, reason, _ = check_travel_pay(
        "Low Pay NY No Field",
        "Lead role. $50/day for 1 day. Shoots in New York.",
    )
    assert ok is False
    assert reason and "fly-to" in reason.lower()


def test_travel_pay_recognizes_dfw_abbreviation_as_fly_to():
    """'DFW' (no 'Dallas' anywhere) must resolve to the fly-to tier, not go undetected."""
    ok, reason, _ = check_travel_pay(
        "Meow Wolf",
        "Please submit DFW locals here. ALL talent must be willing to work as a DFW local hire. "
        "$350/8 hours + 20%",
    )
    assert ok is False, "location should resolve to fly-to and reject pay below $1000, not pass as undetermined"
    assert reason and "fly-to" in reason.lower()


def test_travel_pay_recognizes_santa_fe_as_fly_to():
    """'Santa Fe' (no state name present) must resolve to the fly-to tier, not go undetected."""
    ok, reason, _ = check_travel_pay(
        "Meow Wolf",
        "Please submit Santa Fe locals here. ALL talent must be willing to work as a Santa Fe local hire. "
        "$350/8 hours + 20%",
    )
    assert ok is False, "location should resolve to fly-to and reject pay below $1000, not pass as undetermined"
    assert reason and "fly-to" in reason.lower()


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


# --- info_note tests (casting-suggestion #83 follow-up) ---
#
# #83 fixed the submission_note field's boilerplate, but "PLEASE INCLUDE SIZE
# CARDS" is deliberately handled via the AA profile (not a note) and a demo-clip
# request with no reel on file has nothing to attach — both still leave `note`
# empty, so the digest showed the same "No specific submission info requested"
# text as a listing that asked for nothing at all. info_note is a digest-only
# annotation (never submitted to casting) that distinguishes the two cases.


def test_analyze_size_card_request_sets_info_note():
    """Size card requests get SUBMIT with no note (handled via profile) — the
    digest should say so instead of implying nothing was requested."""
    mock_anthropic, _ = _make_mock_anthropic("ACTION: SUBMIT")
    role = {"role_name": "Model", "description": "PLEASE INCLUDE SIZE CARDS with your submission."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "SUBMIT"
    assert result["note"] is None
    assert result["info_note"] == "Size card requested — on file in AA profile."


def test_analyze_demo_clip_request_with_no_reel_sets_info_note():
    """Demo clip requests when the actor has no reel on file (the default
    ACTOR_PROFILE) should be visible in the digest, not indistinguishable from
    a listing that asked for nothing."""
    mock_anthropic, _ = _make_mock_anthropic("ACTION: SUBMIT")
    role = {"role_name": "Host", "description": "Please submit actor's online demo clips along with submission."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "Test Project", has_media=True)
    assert result["action"] == "SUBMIT"
    assert result["note"] is None
    assert result["info_note"] == "Demo clips requested — no reel on file; applied anyway."


def test_analyze_no_requirements_leaves_info_note_unset():
    """A listing with no special requirements should not get an info_note either."""
    mock_anthropic, _ = _make_mock_anthropic("ACTION: SUBMIT")
    role = {"role_name": "Jake", "description": "Male lead, 25-30."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "Test Project")
    assert result.get("info_note") is None


# --- fabricated contact info guard (casting-suggestion #77) ---


def test_analyze_rejects_fabricated_email_in_note():
    """A note with an invented email (not in ACTOR_PROFILE) must fall back to plain SUBMIT."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: My email is marshallpowell@email.com — please reach out with booking details."
    )
    role = {"role_name": "Photographer", "description": "Please include your email in the submission note."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "SUBMIT"
    assert result["note"] is None


def test_analyze_rejects_fabricated_phone_in_note():
    """A note with an invented phone number must fall back to plain SUBMIT."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: You can reach me at 555-123-4567 anytime."
    )
    role = {"role_name": "Model", "description": "Please include your phone number in the submission note."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "SUBMIT"
    assert result["note"] is None


def test_analyze_rejects_contact_on_file_claim():
    """A note claiming email/phone is 'on file' or 'upon request' is an unfounded claim
    (nothing is on file beyond ACTOR_PROFILE) and must fall back to plain SUBMIT."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: Age 28, 6'0\", Instagram @marshallpowell. Email and cell on file — happy to provide directly if needed."
    )
    role = {"role_name": "Male Model", "description": "Please include your age, height, Instagram, email, and phone."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "SUBMIT"
    assert result["note"] is None


def test_analyze_rejects_contact_number_is_available_claim():
    """'Best contact number is available' is the same unfounded on-file/upon-request
    deflection (casting-suggestion #66) with different wording — must also fall back to
    plain SUBMIT. Reproduces the July 28, 2026 digest 'Norbit Stream' — Fitness trainer
    note, which slipped past the narrower on-file/upon-request/happy-to-provide regex."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: Best contact number is available — please reach out via my submission profile, or find me on Instagram @marshallpowell."
    )
    role = {"role_name": "Fitness trainer", "description": "Please provide your best contact number in case you're selected."}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "Test Project")
    assert result["action"] == "SUBMIT"
    assert result["note"] is None


# --- reel/footage note guard (casting-suggestion: GM Brand Car and Truck Hosts, July 15 2026 digest) ---


def test_analyze_rejects_note_volunteering_missing_reel():
    """Regression: the GMC Experienced Host role (same project as the flagged Cadillac
    Experienced Host role) was auto-submitted with the note "Instagram: @marshallpowell.
    No host reel currently available." — volunteering that the requested hosting reel is
    missing, which the prompt already forbids but the model did anyway. Must fall back to
    a note without the volunteered negative."""
    mock_anthropic, _ = _make_mock_anthropic(
        "ACTION: SUBMIT_WITH_NOTE\nNOTE: Instagram: @marshallpowell. No host reel currently available."
    )
    role = {
        "role_name": "GMC Experienced Host",
        "description": "Hosting experience preferred. Please include your Instagram and a hosting reel.",
    }
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            result = analyze_submission_requirements(role, "GM Brand Car and Truck Hosts")
    assert result["action"] == "SUBMIT"
    assert result["note"] is None


def test_validate_note_allows_note_that_positively_mentions_reel():
    """A note that positively confirms a reel (the pipeline's own "Demo reel attached."
    confirmation, injected elsewhere when has_media=True and clips were requested) must
    not be caught by the missing-reel guard — it's a positive statement, not a volunteered
    negative like "No host reel currently available.\""""
    role = {"role_name": "Jake"}
    assert _validate_note("Demo reel attached.", role, "Test Project") is True
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


# --- _unmet_gender_role_name_conflict (Blue Cross "Woman"/"Man" print role case) ---


def test_gender_role_name_conflict_bare_woman_label_no_inclusive_language():
    assert _unmet_gender_role_name_conflict(
        "Woman", "Warm, friendly. Experience on camera preferred."
    )


def test_gender_role_name_conflict_variants():
    for name in ["Female", "Girl", "Women", "Lady", "Woman #2", "The Woman"]:
        assert _unmet_gender_role_name_conflict(name, "Warm and friendly.")


def test_gender_role_name_no_conflict_with_inclusive_language():
    assert not _unmet_gender_role_name_conflict(
        "Woman", "Warm, friendly. Open to any gender."
    )


def test_gender_role_name_no_conflict_for_non_gender_role_names():
    # Named characters and role types aren't bare gender labels, even if the
    # role happens to be for a woman — this check only fires on role names
    # that are literally the gender word itself.
    assert not _unmet_gender_role_name_conflict("Nurse Ramirez", "Female nurse, 30s.")
    assert not _unmet_gender_role_name_conflict("Man", "Warm, friendly.")


def test_select_best_roles_multi_role_demotes_bare_gender_labeled_role():
    """Blue Cross print project: sibling 'Man'/'Woman' roles share an identical,
    gender-neutral description. The AI selecting both for a male actor (as
    happened on July 13, 2026) must have the 'Woman' pick overridden to a
    rejection — the role's own name is a real exclusion the AI's disqualifier
    scan can't see because the description text has no 'female only' phrase.
    """
    roles = [
        {"role_name": "Woman", "description": "Warm, friendly. Experience on camera preferred."},
        {"role_name": "Man", "description": "Warm, friendly. Experience on camera preferred."},
    ]
    mock_module, mock_client = _make_mock_anthropic(
        "SELECTED: 1 - Print modeling role open to any gender\n"
        "SELECTED: 2 - Warm/friendly type matches his on-camera charm"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Blue Cross")
    selected_names = {r["role_name"] for r, _ in selected}
    assert selected_names == {"Man"}
    assert "Woman" in rejections


def test_check_single_role_fit_demotes_bare_gender_labeled_role():
    roles = [{"role_name": "Woman", "description": "Warm, friendly. Experience on camera preferred."}]
    mock_module, mock_client = _make_mock_anthropic(
        "FIT - Print modeling role open to any gender"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Blue Cross")
    assert selected == []
    assert "Woman" in rejections


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


# --- casting-suggestion #85: ambiguous/unlisted pay must be flagged, not
# silently auto-passed as if it confirmed the threshold was cleared. ---


def test_travel_pay_ambiguous_when_pay_unparseable_at_fly_to_location():
    """Pay listed as 'SEE BREAKDOWN' (no number) at a fly-to location must come
    back as ambiguous — not a silent pass and not a rejection."""
    ok, reason, ambiguous = check_travel_pay(
        "TARGET MINNESOTA",
        "Football Players and Basketball Players. Pay: SEE BREAKDOWN. Shoots in Minneapolis, MN.",
    )
    assert ok is True
    assert reason is None
    assert ambiguous is True


def test_travel_pay_not_ambiguous_when_location_is_la():
    """No threshold applies in LA, so unparseable pay there is not ambiguous."""
    ok, reason, ambiguous = check_travel_pay(
        "LA Short",
        "Supporting role. Pay: SEE BREAKDOWN. Shoots in Los Angeles, CA.",
    )
    assert ok is True
    assert reason is None
    assert ambiguous is False


def test_override_local_hire_not_applied_when_pay_ambiguous():
    """The local-hire override must not claim pay 'clears threshold' when pay
    is genuinely unparseable — that fabricates confidence the check never had.
    Regression: Marriott Miami (Miami local hire, PAY: 'Please see usage/run')
    was overridden with the false claim 'travel pay clears threshold' even
    though the underlying check could not determine any pay amount."""
    role = {
        "role_name": "Miami - Working Professional Man",
        "description": "Must be Miami local hire. Attractive but approachable.",
        "pay": "Please see usage/run",
    }
    ai_reason = (
        "Miami local hire requirement; shoot location is Miami (fly-to), and pay is "
        "unspecified — cannot confirm it meets the $1,000 threshold."
    )
    overridden, new_reason = _maybe_override_local_hire_skip(role, "Marriott", ai_reason, "paid")
    assert overridden is False, f"must not override on ambiguous pay, got: {new_reason}"
    assert new_reason == ai_reason


# --- casting-suggestion #112/#117/#118: bundled non-waivable disqualifiers must
# block the local-hire override even when travel pay clears the threshold. ---


def test_no_override_local_hire_when_bundled_state_residency_requirement():
    """#112: New Orleans SAG feature requiring a Louisiana resident with a valid
    Louisiana state driver's license. Residency / state ID can't be waived by pay."""
    role = {"role_name": "Brian", "description": "Supporting role. Fly to New Orleans, LA. $1200/day for 5 days."}
    ai_reason = (
        "Hard location disqualifier: role requires being a Louisiana local resident "
        "with a valid Louisiana state driver's license; also local hire in New Orleans."
    )
    overridden, new_reason = _maybe_override_local_hire_skip(role, "The Contemptuous Ruby", ai_reason, "paid")
    assert overridden is False
    assert new_reason == ai_reason


def test_no_override_local_hire_when_bundled_language_fluency_requirement():
    """#117: role requires fluent Italian, bundled with an NY/NJ local-hire objection."""
    role = {"role_name": "Enzo", "description": "Warm Italian manager. Fly to New York. $2000 total."}
    ai_reason = (
        "Role requires fluent Italian (actor does not speak Italian); also requires "
        "NY/NJ local hire and pay of $834/day."
    )
    overridden, new_reason = _maybe_override_local_hire_skip(role, "The Method", ai_reason, "paid")
    assert overridden is False
    assert new_reason == ai_reason


def test_no_override_local_hire_when_bundled_real_runners_plural():
    """#118: 'authentically skilled at running' / plural 'REAL ... RUNNERS' is a real
    athletic-skill requirement the actor lacks, bundled with a Denver local-hire objection."""
    role = {"role_name": "Road Runner", "description": "Fly to Denver. $1500 total."}
    ai_reason = (
        "Requires talent authentically skilled at running on pavement; also Denver "
        "local hire, fly-to location."
    )
    overridden, new_reason = _maybe_override_local_hire_skip(role, "HOKA Denver", ai_reason, "paid")
    assert overridden is False
    assert new_reason == ai_reason


def test_override_local_hire_still_fires_for_plain_local_hire_with_clearing_pay():
    """Guard regression: a pure local-hire objection with clearing pay and no bundled
    non-waivable disqualifier must STILL override (broadened patterns must not false-positive)."""
    role = {"role_name": "Guy", "description": "Fly to New York. $2000 total for the shoot."}
    ai_reason = "Requires NY local hire; shoot is a fly-to location with no reimbursement."
    overridden, _ = _maybe_override_local_hire_skip(role, "Some Project", ai_reason, "paid")
    assert overridden is True


# --- casting-suggestion #105: single-point age-boundary touch is not a real overlap ---


def test_age_overlap_override_not_fired_on_single_point_boundary():
    """#105 (Weller Bourbon): role '30-45' shares only age 30 with the actor's
    17-30 range. That single-point touch must NOT override the AI's own
    'cannot credibly play 30+ as a minimum' credibility judgment."""
    from src.role_selector import _maybe_override_age_overlap_skip
    role = {"role_name": "Pappy", "age_range": "30-45"}
    ai_reason = (
        "Age range is 30-45 with no overlap with actor's 17-30 range; actor "
        "cannot credibly play 30+ as a minimum."
    )
    overridden, new_reason = _maybe_override_age_overlap_skip(role, ai_reason)
    assert overridden is False
    assert new_reason == ai_reason


def test_age_overlap_override_still_fires_on_genuine_multi_year_window():
    """Regression for #70: a real 2-year overlap window (28-38 vs 17-30 shares
    28-30) must still override the AI's mistaken 'no overlap' claim."""
    from src.role_selector import _maybe_override_age_overlap_skip
    role = {"role_name": "Sam", "age_range": "28-38"}
    ai_reason = "Age range 28-38, no overlap with actor's 17-30 playable range."
    overridden, _ = _maybe_override_age_overlap_skip(role, ai_reason)
    assert overridden is True


# --- casting-suggestion #109: per-project 3-role submission cap needs a code backstop ---


def test_paid_mode_caps_selected_at_three_roles():
    """#109 (Spaghetti project, 5 of 6 applied): when the AI ignores the soft
    'no more than 3' instruction, a code-level backstop must cap paid submissions."""
    roles = [
        {"role_name": f"Role{i}", "role_type": "Supporting", "gender": "Male",
         "age_range": "20-30", "description": "Friendly everyday guy, no special skills."}
        for i in range(1, 7)
    ]
    response = "\n".join(f"SELECTED: {i} - Good fit, age and type match" for i in range(1, 6)) \
        + "\nREJECTED: 6 - Not a fit"
    mock_module, _ = _make_mock_anthropic(response)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Spaghetti", mode="paid")
    assert len(selected) == 3
    capped = [n for n, r in rejections.items() if "Submission cap" in r]
    assert len(capped) == 2


def test_unpaid_mode_does_not_cap_selected():
    """Unpaid mode intentionally selects ALL reasonable fits — the cap is paid-only."""
    roles = [
        {"role_name": f"Role{i}", "role_type": "Supporting", "gender": "Male",
         "age_range": "20-30", "description": "Friendly everyday guy, no special skills."}
        for i in range(1, 7)
    ]
    response = "\n".join(f"SELECTED: {i} - Good fit" for i in range(1, 6)) + "\nREJECTED: 6 - Not a fit"
    mock_module, _ = _make_mock_anthropic(response)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(roles, "Spaghetti", mode="unpaid")
    assert len(selected) == 5


# --- casting-suggestion #110/#113: a role the AI omitted must not show a raw marker ---


def test_omitted_role_gets_human_readable_reason_not_raw_marker():
    """A role neither SELECTED nor REJECTED by the AI must be filed with an honest
    'not evaluated' reason, never the internal 'not mentioned by AI' string."""
    response = "SELECTED: 1 - Age and type match\nREJECTED: 2 - Requires heavyset build"
    mock_module, _ = _make_mock_anthropic(response)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_module}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")
    assert "Tommy" in rejections
    assert "not mentioned by AI" not in rejections["Tommy"]
    assert "not evaluated" in rejections["Tommy"].lower()
