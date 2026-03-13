# tests/test_digest.py
"""Tests for the daily digest email builder.

Tests the data gathering and HTML rendering. Does NOT test SendGrid sending.
"""
import os
import time
from unittest.mock import patch, MagicMock

import pytest

from src.database import Database
from src.digest import build_digest_html, gather_digest_data


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
