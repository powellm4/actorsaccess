# tests/backstage/test_overrides.py
"""Tests for the Apply Anyway override flow in src/backstage/main.py.

Backstage submits via REST: fetch_role_detail(url) → role_id → submit_for_role.
The BackstageClient is fully mocked here.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from src.backstage import main as bs_main
from src.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(os.path.join(str(tmp_path), "t.db"))


@pytest.fixture
def cfg():
    return {
        "submission": {"video_reel_ids": [10], "resume_id": 99},
        "overrides": {"repo": "o/r", "label": "apply-anyway"},
    }


@pytest.fixture
def fake_client():
    return MagicMock()


def _seed_bs_url(db, project_name, role_name, role_url):
    """Insert a rejected_roles row so get_known_project_url returns role_url."""
    run_id = db.start_run(platform="backstage", mode="paid")
    db.record_rejection(
        project_name=project_name, project_url=role_url,
        role_name=role_name, role_description="d", rejection_reason="ai",
        run_id=run_id, platform="backstage",
    )


def test_apply_backstage_override_no_role_url_marks_failed(db, cfg, fake_client):
    db.add_pending_override(1, "Mystery", "Lead", "backstage", "paid")
    override = db.get_pending_override("Mystery", "Lead", "backstage", "paid")

    with patch("src.backstage.main.overrides_mod"):
        bs_main._apply_backstage_override(
            cfg, db, fake_client, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    outcomes = db.get_daily_override_outcomes()
    assert outcomes[0]["outcome"] == "failed"
    assert "URL not on file" in outcomes[0]["detail"]
    fake_client.fetch_role_detail.assert_not_called()
    fake_client.submit_for_role.assert_not_called()


def test_apply_backstage_override_role_detail_none_marks_not_found(db, cfg, fake_client):
    _seed_bs_url(db, "P", "R", "https://www.backstage.com/casting/x/")
    db.add_pending_override(2, "P", "R", "backstage", "paid")
    override = db.get_pending_override("P", "R", "backstage", "paid")

    fake_client.fetch_role_detail.return_value = None  # Cloudflare or page gone

    with patch("src.backstage.main.overrides_mod"):
        bs_main._apply_backstage_override(
            cfg, db, fake_client, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    assert db.get_daily_override_outcomes()[0]["outcome"] == "not_found"
    fake_client.submit_for_role.assert_not_called()


def test_apply_backstage_override_role_name_not_in_production_marks_not_found(
    db, cfg, fake_client,
):
    _seed_bs_url(db, "P", "Lead", "https://www.backstage.com/casting/x/")
    db.add_pending_override(3, "P", "Lead", "backstage", "paid")
    override = db.get_pending_override("P", "Lead", "backstage", "paid")

    fake_client.fetch_role_detail.return_value = {
        "id": 500,
        "roles": [{"id": 600, "name": "Different Role"}],
    }

    with patch("src.backstage.main.overrides_mod"):
        bs_main._apply_backstage_override(
            cfg, db, fake_client, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    assert db.get_daily_override_outcomes()[0]["outcome"] == "not_found"
    fake_client.submit_for_role.assert_not_called()


def test_apply_backstage_override_happy_path_records_application(db, cfg, fake_client):
    role_url = "https://www.backstage.com/casting/x/"
    _seed_bs_url(db, "Acme", "Hero", role_url)
    db.add_pending_override(4, "Acme", "Hero", "backstage", "paid")
    override = db.get_pending_override("Acme", "Hero", "backstage", "paid")

    fake_client.fetch_role_detail.return_value = {
        "id": 500,
        "roles": [{"id": 600, "name": "Hero"}],
    }
    fake_client.submit_for_role.return_value = {"id": 12345, "status": "C"}

    with patch("src.backstage.main.overrides_mod") as om:
        bs_main._apply_backstage_override(
            cfg, db, fake_client, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    assert db.is_applied("backstage_500_600") is True
    apps = db.get_daily_applications()
    assert apps[0]["platform"] == "backstage"
    assert "OVERRIDE" in apps[0]["ai_reason"]
    assert db.is_rejected("Hero", "Acme", "backstage") is False
    assert db.get_daily_override_outcomes()[0]["outcome"] == "applied"
    assert db.list_pending_overrides() == []
    om.comment_and_close.assert_called_once()

    # media_ids combine video_reel_ids + resume_id from cfg["submission"].
    call_kwargs = fake_client.submit_for_role.call_args.kwargs
    assert call_kwargs["media_ids"] == [10, 99]
    assert call_kwargs["note"] == ""
    assert call_kwargs["answers"] is None


def test_apply_backstage_override_rejected_by_server_marks_failed(db, cfg, fake_client):
    _seed_bs_url(db, "P", "R", "https://www.backstage.com/casting/x/")
    db.add_pending_override(5, "P", "R", "backstage", "paid")
    override = db.get_pending_override("P", "R", "backstage", "paid")

    fake_client.fetch_role_detail.return_value = {
        "id": 1, "roles": [{"id": 2, "name": "R"}],
    }
    fake_client.submit_for_role.return_value = {"_rejected": True, "reason": "age range"}

    with patch("src.backstage.main.overrides_mod"):
        bs_main._apply_backstage_override(
            cfg, db, fake_client, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    outcomes = db.get_daily_override_outcomes()
    assert outcomes[0]["outcome"] == "failed"
    assert "age range" in outcomes[0]["detail"]
    # Rejection stays so the role still surfaces in Passed.
    assert db.is_rejected("R", "P", "backstage") is True


def test_apply_backstage_override_dry_run_skips_submit(db, cfg, fake_client):
    _seed_bs_url(db, "P", "R", "https://www.backstage.com/casting/x/")
    db.add_pending_override(6, "P", "R", "backstage", "paid")
    override = db.get_pending_override("P", "R", "backstage", "paid")

    fake_client.fetch_role_detail.return_value = {
        "id": 1, "roles": [{"id": 2, "name": "R"}],
    }

    with patch("src.backstage.main.overrides_mod"):
        bs_main._apply_backstage_override(
            cfg, db, fake_client, override,
            cfg["overrides"], "tok", dry_run=True,
        )

    fake_client.submit_for_role.assert_not_called()
    # Queue preserved so the real run picks it up.
    assert len(db.list_pending_overrides()) == 1


def test_process_backstage_overrides_routing(db, cfg, fake_client):
    _seed_bs_url(db, "P1", "R1", "https://www.backstage.com/casting/x/")
    db.add_pending_override(1, "P1", "R1", "backstage", "paid")
    db.add_pending_override(2, "P2", "R2", "backstage", "unpaid")  # different mode
    db.add_pending_override(3, "P3", "R3", "cn", "paid")  # different platform

    fake_client.fetch_role_detail.return_value = {
        "id": 50, "roles": [{"id": 51, "name": "R1"}],
    }
    fake_client.submit_for_role.return_value = {"id": 99}

    with patch.dict(os.environ, {"OVERRIDE_GITHUB_TOKEN": "tok"}), \
         patch("src.backstage.main.overrides_mod") as om:
        om.load_run_config.return_value = (cfg["overrides"], "tok")
        om.fetch_pending_with_errors.return_value = ([], [])
        bs_main.process_backstage_overrides(
            cfg, db, fake_client, run_id=42, mode="paid", dry_run=False,
        )

    assert fake_client.submit_for_role.call_count == 1
    remaining = {(o["platform"], o["mode"]) for o in db.list_pending_overrides()}
    assert ("backstage", "unpaid") in remaining
    assert ("cn", "paid") in remaining
