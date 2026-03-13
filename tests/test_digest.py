# tests/test_digest.py
"""Tests for the daily digest email builder.

Tests the data gathering and HTML rendering. Does NOT test SendGrid sending.
"""
import os
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
    data = {"applications": [], "rejections": [], "runs": []}
    html = build_digest_html(data)
    assert "No applications" in html or "no applications" in html
