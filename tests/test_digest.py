# tests/test_digest.py
"""Tests for the daily digest email builder.

Tests the data gathering and HTML rendering. Does NOT test SendGrid sending.
"""
import os
import time
from unittest.mock import patch, MagicMock

import pytest

from src.database import Database
from src.digest import (
    _ensure_override_label,
    build_digest_html,
    build_email_message,
    gather_digest_data,
)


@pytest.fixture
def db(tmp_path):
    db_path = os.path.join(str(tmp_path), "test.db")
    return Database(db_path)


def test_gather_digest_data_empty(db):
    """Empty DB should return empty data."""
    data = gather_digest_data(db)
    assert data["applications"] == []
    assert data["rejections"] == []
    assert data["runs"] == []


def test_gather_digest_data_with_records(db):
    """Should gather today's applications and rejections."""
    run_id = db.start_run()
    db.record_application(
        "role_1", "Test Project", "Lead",
        ai_reason="Best fit", project_url="https://example.com",
    )
    db.record_rejection(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Age too high",
        run_id=run_id,
        platform="aa",
    )
    db.complete_run(run_id, roles_found=5, roles_applied=1, roles_skipped=2)

    data = gather_digest_data(db)
    assert len(data["applications"]) == 1
    assert len(data["rejections"]) == 1
    assert len(data["runs"]) == 1


def test_build_digest_html_with_data(db):
    """Should produce HTML with project sections."""
    run_id = db.start_run()
    db.record_application(
        "role_1", "Test Project", "Lead",
        ai_reason="Best fit", project_url="https://example.com",
    )
    db.record_rejection(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Age too high",
        run_id=run_id,
        platform="aa",
    )
    db.complete_run(run_id, roles_found=5, roles_applied=1, roles_skipped=2)

    data = gather_digest_data(db)
    html = build_digest_html(data)

    assert "Test Project" in html
    assert "Lead" in html
    assert "Best fit" in html
    assert "Villain" in html
    assert "Age too high" in html
    assert "https://example.com" in html


def test_build_digest_html_empty():
    """Empty data should produce 'no applications' message."""
    data = {"applications": [], "rejections": [], "flagged": [], "runs": []}
    html = build_digest_html(data)
    assert "No applications" in html or "no applications" in html


def test_gather_digest_data_includes_flagged(db):
    """Digest data should include flagged roles."""
    run_id = db.start_run()
    db.record_flagged_role(
        project_name="Flagged Project",
        project_url="https://example.com/flagged",
        role_name="Hero",
        role_description="An action hero",
        flag_reason="Needs specific availability dates",
        run_id=run_id,
        platform="aa",
    )
    data = gather_digest_data(db)
    assert len(data["flagged"]) == 1
    assert data["flagged"][0]["role_name"] == "Hero"


def test_digest_only_shows_since_last_digest(db):
    """After a digest is sent, only new roles should appear."""
    run_id = db.start_run()
    db.record_application(
        "role_old", "Old Project", "Old Role",
        ai_reason="Match", project_url="https://example.com",
    )
    db.complete_run(run_id, roles_found=1, roles_applied=1, roles_skipped=0)

    # Simulate sending a digest
    time.sleep(0.05)
    db.record_digest_sent()

    # Before any new activity, digest should be empty
    data = gather_digest_data(db)
    assert len(data["applications"]) == 0
    assert len(data["runs"]) == 0

    # New activity after digest
    time.sleep(0.05)
    run_id2 = db.start_run()
    db.record_application(
        "role_new", "New Project", "New Role",
        ai_reason="Great fit", project_url="https://example.com/new",
    )
    db.complete_run(run_id2, roles_found=1, roles_applied=1, roles_skipped=0)

    data = gather_digest_data(db)
    assert len(data["applications"]) == 1
    assert data["applications"][0]["project_name"] == "New Project"
    assert len(data["runs"]) == 1


def test_build_digest_html_with_flagged():
    """Flagged roles should appear in 'Needs Your Attention' section."""
    data = {
        "applications": [],
        "rejections": [],
        "flagged": [
            {
                "project_name": "Flagged Project",
                "project_url": "https://example.com/flagged",
                "role_name": "Hero",
                "role_description": "An action hero",
                "flag_reason": "Needs SAG-AFTRA number",
                "platform": "aa",
                "flagged_at": "2026-03-12 10:00:00",
            }
        ],
        "runs": [],
    }
    html = build_digest_html(data)
    assert "Needs Your Attention" in html
    assert "Flagged Project" in html
    assert "Hero" in html
    assert "Needs SAG-AFTRA number" in html
    assert "https://example.com/flagged" in html


def test_build_digest_html_with_draft_renders_open_link_and_suggested_note():
    """Prepare-only Backstage drafts should surface the Open on Backstage link
    and the AI-suggested cover letter."""
    data = {
        "applications": [],
        "rejections": [],
        "flagged": [
            {
                "project_name": "ANTA Running Shoes Promo",
                "project_url": "https://www.backstage.com/casting/123/",
                "role_name": "Male Fitness Model",
                "role_description": "Beverly Hills lifestyle shoot",
                "flag_reason": "Cover letter required (Headshot/Photo, Cover Letter) — draft ready in Backstage",
                "platform": "backstage",
                "flagged_at": "2026-04-24 10:00:00",
                "suggested_note": "The Beverly Hills lifestyle brief feels right in my lane.",
                "draft_app_id": 987654,
            }
        ],
        "runs": [],
    }
    html = build_digest_html(data)
    assert "Open on Backstage" in html
    assert "https://www.backstage.com/casting/123/" in html
    assert "Suggested cover letter" in html
    assert "Beverly Hills lifestyle brief" in html


def test_build_digest_html_flag_without_draft_has_no_open_link():
    """Flagged rows without a draft_app_id (e.g., other needs_input cases) must
    NOT render the 'Open on Backstage' CTA."""
    data = {
        "applications": [],
        "rejections": [],
        "flagged": [
            {
                "project_name": "Some Project",
                "project_url": "https://example.com",
                "role_name": "Some Role",
                "role_description": "",
                "flag_reason": "Needs demo reel",
                "platform": "backstage",
                "flagged_at": "2026-04-24 10:00:00",
            }
        ],
        "runs": [],
    }
    html = build_digest_html(data)
    assert "Open on Backstage" not in html
    assert "Suggested cover letter" not in html


def test_build_digest_html_attachment_callout_when_no_site_url(monkeypatch):
    """Without ARCHIVE_SITE_URL set, digest body promotes the offline attachment."""
    monkeypatch.delenv("ARCHIVE_SITE_URL", raising=False)
    data = {"applications": [], "rejections": [], "flagged": [], "runs": []}
    html = build_digest_html(data)
    assert "submissions-archive.html" in html
    assert "Searchable archive" in html


def test_build_digest_html_links_to_site_when_url_set(monkeypatch):
    """When ARCHIVE_SITE_URL is set, the body should link to the live site
    instead of mentioning the attachment."""
    monkeypatch.setenv("ARCHIVE_SITE_URL", "https://powellm4.github.io/actorsaccess/")
    data = {"applications": [], "rejections": [], "flagged": [], "runs": []}
    html = build_digest_html(data)
    assert "https://powellm4.github.io/actorsaccess/" in html
    assert "Open submissions archive" in html
    assert "submissions-archive.html" not in html


def test_build_email_message_attaches_archive():
    """When archive_html is supplied, the message has an HTML attachment."""
    msg = build_email_message(
        html="<p>body</p>", mode="paid", sender="me@example.com",
        archive_html="<html><body>archive</body></html>",
    )
    parts = list(msg.walk())
    attachments = [
        p for p in parts
        if p.get_content_disposition() == "attachment"
    ]
    assert len(attachments) == 1
    att = attachments[0]
    assert att.get_filename() == "submissions-archive.html"
    assert att.get_content_type() == "text/html"
    assert b"archive" in att.get_payload(decode=True)


def test_build_email_message_no_archive_omits_attachment():
    """Without archive_html, no attachment should be added."""
    msg = build_email_message(
        html="<p>body</p>", mode="paid", sender="me@example.com",
    )
    attachments = [
        p for p in msg.walk()
        if p.get_content_disposition() == "attachment"
    ]
    assert attachments == []


def test_manually_applied_section_appears_at_top_of_digest():
    """Manually-applied results must render BEFORE Needs Attention / Passed /
    Applied. The user explicitly opened an issue for each, and burying the
    outcome under other sections defeats the confirmation purpose.
    """
    data = {
        "applications": [
            {
                "project_name": "Other Project", "project_url": "",
                "role_name": "Lead", "role_description": "",
                "ai_reason": "Great fit", "platform": "aa",
                "candidates_considered": 1,
            }
        ],
        "rejections": [],
        "flagged": [
            {
                "project_name": "Flagged Project", "project_url": "",
                "role_name": "Hero", "role_description": "",
                "flag_reason": "Needs SAG-AFTRA number", "platform": "aa",
                "flagged_at": "2026-04-24 10:00:00",
            }
        ],
        "overrides": [
            {
                "issue_number": 42, "project_name": "Override Project",
                "role_name": "Hero", "platform": "aa", "mode": "paid",
                "outcome": "applied", "detail": "",
                "processed_at": "2026-04-24 11:00:00",
            }
        ],
        "runs": [],
    }
    html = build_digest_html(data)

    idx_manually = html.find("Apply Anyway Results")
    idx_attention = html.find("Needs Your Attention")
    idx_applied = html.find(">Applied<")

    assert idx_manually != -1, "Apply Anyway Results section must render when an override exists"
    assert idx_attention != -1
    assert idx_applied != -1
    assert idx_manually < idx_attention
    assert idx_manually < idx_applied


def test_passed_roles_appear_above_applied_in_digest():
    """The review block (calendar / flagged / passed) must render above the
    'Applied' section so the user sees what to review without scrolling past
    successful submissions."""
    data = {
        "applications": [
            {
                "project_name": "Applied Project",
                "project_url": "https://example.com/applied",
                "role_name": "Lead",
                "role_description": "Hero",
                "ai_reason": "Great fit",
                "platform": "aa",
                "candidates_considered": 1,
            }
        ],
        "rejections": [
            {
                "project_name": "Passed Project",
                "project_url": "https://example.com/passed",
                "role_name": "Villain",
                "role_description": "Bad guy",
                "rejection_reason": "Age too high",
                "platform": "backstage",
            }
        ],
        "flagged": [
            {
                "project_name": "Flagged Project",
                "project_url": "https://example.com/flagged",
                "role_name": "Hero",
                "role_description": "An action hero",
                "flag_reason": "Needs SAG-AFTRA number",
                "platform": "aa",
                "flagged_at": "2026-04-24 10:00:00",
            },
            {
                "project_name": "Conflict Project",
                "project_url": "https://example.com/conflict",
                "role_name": "Extra",
                "role_description": "",
                "flag_reason": "Calendar conflict: Wedding",
                "platform": "backstage",
                "flagged_at": "2026-04-24 10:00:00",
            },
        ],
        "runs": [],
    }
    html = build_digest_html(data)

    # Top-of-email review headings must precede the Applied block.
    idx_calendar = html.find("Skipped — Calendar Conflicts")
    idx_attention = html.find("Needs Your Attention")
    idx_passed_heading = html.find(">Passed<")
    idx_passed_card = html.find("PASSED")
    idx_applied_heading = html.find(">Applied<")
    idx_applied_card = html.find("APPLIED")

    assert idx_calendar != -1
    assert idx_attention != -1
    assert idx_passed_heading != -1
    assert idx_passed_card != -1
    assert idx_applied_heading != -1
    assert idx_applied_card != -1

    assert idx_calendar < idx_attention < idx_passed_heading
    assert idx_passed_card < idx_applied_card
    assert idx_passed_heading < idx_applied_heading


def test_passed_section_includes_project_name_and_no_inline_passed_in_applied():
    """Each PASSED card should carry its project name (since cards are no
    longer nested inside per-project sections), and the per-project Applied
    sections must contain zero PASSED cards."""
    data = {
        "applications": [
            {
                "project_name": "Shared Project",
                "project_url": "https://example.com",
                "role_name": "Lead",
                "role_description": "",
                "ai_reason": "Great fit",
                "platform": "aa",
                "candidates_considered": 1,
            }
        ],
        "rejections": [
            {
                "project_name": "Shared Project",
                "project_url": "https://example.com",
                "role_name": "Villain",
                "role_description": "",
                "rejection_reason": "Age",
                "platform": "aa",
            }
        ],
        "flagged": [],
        "runs": [],
    }
    html = build_digest_html(data)

    # Locate the per-project applied block (bordered card).
    applied_block_start = html.find('border:1px solid #ddd')
    assert applied_block_start != -1
    applied_block = html[applied_block_start:]
    assert "PASSED" not in applied_block
    assert "Villain" not in applied_block

    # The passed card should reference its project so the user can still
    # tell where it came from in the flat list.
    passed_block = html[: applied_block_start]
    assert "Shared Project" in passed_block
    assert "Villain" in passed_block


def test_build_digest_html_calendar_conflict_has_no_draft_link():
    """Calendar-conflict flags must stay in their own red section without
    any draft-related affordances, even if suggested_note somehow leaks in."""
    data = {
        "applications": [],
        "rejections": [],
        "flagged": [
            {
                "project_name": "Conflicted Project",
                "project_url": "https://example.com",
                "role_name": "Lead",
                "role_description": "",
                "flag_reason": "Calendar conflict: Wedding",
                "platform": "backstage",
                "flagged_at": "2026-04-24 10:00:00",
            }
        ],
        "runs": [],
    }
    html = build_digest_html(data)
    assert "Skipped — Calendar Conflicts" in html
    assert "Open on Backstage" not in html


# --- Apply Anyway override links ---

OVERRIDES_CFG = {"repo": "powellm4/aa-overrides", "label": "apply-anyway"}


def _passed_role(name="Lead", project="Acme", platform="aa"):
    return {
        "project_name": project, "project_url": "https://aa.example/?breakdown=1",
        "role_name": name, "role_description": "", "rejection_reason": "AI: not a fit",
        "platform": platform, "mode": "paid",
    }


def _flagged_role(name="Lead", project="Acme", platform="aa"):
    return {
        "project_name": project, "project_url": "https://aa.example/?breakdown=2",
        "role_name": name, "role_description": "", "flag_reason": "Needs cover letter",
        "platform": platform, "mode": "paid", "flagged_at": "2026-04-24 10:00:00",
    }


def test_passed_card_includes_apply_anyway_link_when_overrides_configured():
    data = {"applications": [], "rejections": [_passed_role()], "flagged": [], "runs": []}
    html = build_digest_html(data, overrides_cfg=OVERRIDES_CFG)
    assert "Apply anyway" in html
    # Link routes through /login (return_to the prefilled issue) so a
    # signed-out tap on the private repo gets a sign-in page, not a 404.
    assert "www.github.com/login?return_to=%2Fpowellm4%2Faa-overrides%2Fissues%2Fnew" in html
    # The label and the role identifiers must be in the (encoded) return_to.
    assert "labels%3Dapply-anyway" in html
    assert "Acme" in html
    assert "Lead" in html


def test_flagged_card_includes_apply_anyway_link_when_overrides_configured():
    data = {"applications": [], "rejections": [], "flagged": [_flagged_role()], "runs": []}
    html = build_digest_html(data, overrides_cfg=OVERRIDES_CFG)
    assert "Apply anyway" in html
    assert "www.github.com/login?return_to=%2Fpowellm4%2Faa-overrides%2Fissues%2Fnew" in html


def test_calendar_conflict_card_includes_apply_anyway_link():
    """User explicitly chose 'skip the flag check, submit plainly' for needs-attention.
    Calendar-conflict flags belong to the same flagged_roles table — they should
    expose the same Apply Anyway action."""
    data = {
        "applications": [],
        "rejections": [],
        "flagged": [{
            **_flagged_role(),
            "flag_reason": "Calendar conflict: Wedding",
        }],
        "runs": [],
    }
    html = build_digest_html(data, overrides_cfg=OVERRIDES_CFG)
    assert "Apply anyway" in html


def test_ensure_override_label_creates_label_when_token_present():
    """The digest can be sent before the bot's ingest creates the label.
    Since GitHub 404s a prefilled new-issue URL whose label doesn't exist,
    the digest path must guarantee the label is present first."""
    with patch.dict(os.environ, {"OVERRIDE_GITHUB_TOKEN": "tok"}), \
            patch("src.digest.ensure_label_exists") as mock_ensure:
        _ensure_override_label(OVERRIDES_CFG)
    mock_ensure.assert_called_once_with("powellm4/aa-overrides", "apply-anyway", "tok")


def test_ensure_override_label_noop_without_token():
    env = {k: v for k, v in os.environ.items() if k != "OVERRIDE_GITHUB_TOKEN"}
    with patch.dict(os.environ, env, clear=True), \
            patch("src.digest.ensure_label_exists") as mock_ensure:
        _ensure_override_label(OVERRIDES_CFG)
    mock_ensure.assert_not_called()


def test_ensure_override_label_swallows_errors():
    """Never block sending the digest if label creation fails."""
    with patch.dict(os.environ, {"OVERRIDE_GITHUB_TOKEN": "tok"}), \
            patch("src.digest.ensure_label_exists", side_effect=RuntimeError("boom")):
        _ensure_override_label(OVERRIDES_CFG)  # must not raise


def test_no_apply_anyway_links_when_overrides_not_configured():
    """If overrides_cfg is omitted, the digest renders cleanly without buttons —
    no broken links, no leftover text."""
    data = {
        "applications": [],
        "rejections": [_passed_role()],
        "flagged": [_flagged_role()],
        "runs": [],
    }
    html = build_digest_html(data)  # no overrides_cfg
    assert "Apply anyway" not in html
    assert "github.com/" not in html or "issues/new" not in html


# --- Manually Applied section ---

def test_manually_applied_section_renders_outcomes():
    data = {
        "applications": [],
        "rejections": [],
        "flagged": [],
        "runs": [],
        "overrides": [
            {"issue_number": 7, "project_name": "Acme", "role_name": "Lead",
             "platform": "aa", "mode": "paid",
             "outcome": "applied", "detail": "Submitted successfully",
             "processed_at": "2026-05-10 12:00:00"},
            {"issue_number": 8, "project_name": "Bee", "role_name": "Hero",
             "platform": "aa", "mode": "paid",
             "outcome": "failed", "detail": "Submit button not found",
             "processed_at": "2026-05-10 12:01:00"},
            {"issue_number": 9, "project_name": "Cee", "role_name": "Villain",
             "platform": "aa", "mode": "paid",
             "outcome": "not_found", "detail": "Project no longer visible",
             "processed_at": "2026-05-10 12:02:00"},
        ],
    }
    html = build_digest_html(data, overrides_cfg=OVERRIDES_CFG)
    # Section header explicitly names the user-facing action so it's not
    # mistaken for an "auto-applied" section.
    assert "Apply Anyway Results" in html
    # Per-card pill makes each row obviously an override outcome.
    assert "APPLY ANYWAY" in html
    # Summary line tells the user how many overrides this section covers.
    assert "3 overrides processed" in html
    # Mixed outcomes → summary also calls out the applied count.
    assert "1 applied" in html
    # Each role surfaces by name.
    assert "Lead" in html
    assert "Hero" in html
    assert "Villain" in html
    # Outcomes show through.
    assert "Applied" in html or "applied" in html
    assert "Submit button not found" in html or "Failed" in html
    assert "Project no longer visible" in html or "not visible" in html.lower()
    # Each row links back to its GitHub issue.
    assert "github.com/powellm4/aa-overrides/issues/7" in html
    assert "github.com/powellm4/aa-overrides/issues/8" in html


def test_manually_applied_section_omitted_when_no_overrides():
    """If there are no override outcomes for the window, the section header
    must not render (don't spam the email with empty sections)."""
    data = {
        "applications": [], "rejections": [], "flagged": [], "runs": [],
        "overrides": [],
    }
    html = build_digest_html(data, overrides_cfg=OVERRIDES_CFG)
    assert "Apply Anyway Results" not in html


def test_gather_digest_data_includes_overrides(db):
    """gather_digest_data should populate 'overrides' from override_history."""
    db.record_override_outcome(
        issue_number=1, project_name="P", role_name="R",
        platform="aa", mode="paid", outcome="applied", detail="ok",
    )
    data = gather_digest_data(db)
    assert "overrides" in data
    assert len(data["overrides"]) == 1
    assert data["overrides"][0]["outcome"] == "applied"
