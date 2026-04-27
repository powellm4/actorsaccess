# tests/test_digest.py
"""Tests for the daily digest email builder.

Tests the data gathering and HTML rendering. Does NOT test SendGrid sending.
"""
import os
import time
from unittest.mock import patch, MagicMock

import pytest

from src.database import Database
from src.digest import build_digest_html, build_email_message, gather_digest_data


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


def test_build_digest_html_includes_archive_footer():
    """Digest body must mention the archive attachment so the user knows it's there."""
    data = {"applications": [], "rejections": [], "flagged": [], "runs": []}
    html = build_digest_html(data)
    assert "submissions-archive.html" in html
    assert "Searchable archive" in html


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
