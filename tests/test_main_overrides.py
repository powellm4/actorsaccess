# tests/test_main_overrides.py
"""Tests for the Apply Anyway override orchestration in src/main.py.

The browser, GitHub API, and config loader are all mocked — these tests
exercise the decision flow (fetch → queue → apply / skip / fail / not-found),
not real I/O.
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# main.py imports playwright transitively via src.browser. Tests don't need
# the real browser, so stub the playwright import surface before loading main.
if "playwright" not in sys.modules:
    playwright_pkg = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    for name in ("sync_playwright", "Page", "Browser", "BrowserContext"):
        setattr(sync_api, name, type(name, (), {}))
    playwright_pkg.sync_api = sync_api
    sys.modules["playwright"] = playwright_pkg
    sys.modules["playwright.sync_api"] = sync_api

from src import main as main_mod  # noqa: E402
from src import overrides as overrides_mod  # noqa: E402
from src.database import Database  # noqa: E402
from src.overrides import OverrideRequest  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return Database(os.path.join(str(tmp_path), "t.db"))


@pytest.fixture
def cfg():
    return {
        "submission": {"default_note": "should be cleared", "headshot_index": 0},
        "overrides": {"repo": "o/r", "label": "apply-anyway"},
    }


@pytest.fixture
def fake_browser():
    """A MagicMock standing in for ActorsAccessBrowser."""
    return MagicMock()


# --- load_run_config gating ---

def test_override_config_returns_none_when_section_missing():
    cfg = {"submission": {}}
    with patch.dict(os.environ, {"OVERRIDE_GITHUB_TOKEN": "tok"}):
        assert overrides_mod.load_run_config(cfg) == (None, None)


def test_override_config_returns_none_when_token_missing(cfg):
    with patch.dict(os.environ, {}, clear=True):
        assert overrides_mod.load_run_config(cfg) == (None, None)


def test_override_config_returns_pair_when_present(cfg):
    with patch.dict(os.environ, {"OVERRIDE_GITHUB_TOKEN": "tok"}):
        result = overrides_mod.load_run_config(cfg)
    assert result[0] == cfg["overrides"]
    assert result[1] == "tok"


# --- ingest_issues queues and closes ---

def test_ingest_queues_valid_and_closes_malformed(db, cfg):
    overrides_cfg = cfg["overrides"]
    valid = OverrideRequest(
        issue_number=11, project_name="P", role_name="R",
        platform="aa", mode="paid",
    )
    # Patch the helpers in src.overrides — those names are what the
    # ingest_issues body resolves to.
    with patch("src.overrides.fetch_pending_with_errors") as fp, \
         patch("src.overrides.comment_and_close") as cc, \
         patch("src.overrides.ensure_label_exists"):
        fp.return_value = ([valid], [42])
        overrides_mod.ingest_issues(overrides_cfg, "tok", db)

    # Valid issue queued.
    row = db.get_pending_override("P", "R", "aa", "paid")
    assert row is not None
    assert row["issue_number"] == 11

    # Both valid + malformed got commented + closed.
    closed_issues = [c.args[1] for c in cc.call_args_list]
    assert 11 in closed_issues
    assert 42 in closed_issues


def test_ingest_swallows_fetch_errors(db, cfg):
    """Network failure on GitHub fetch must not abort the run."""
    with patch("src.overrides.fetch_pending_with_errors") as fp, \
         patch("src.overrides.ensure_label_exists"):
        fp.side_effect = RuntimeError("boom")
        # Should not raise.
        overrides_mod.ingest_issues(cfg["overrides"], "tok", db)
    assert db.list_pending_overrides() == []


# --- _apply_aa_override outcomes ---

def test_apply_override_no_project_url_marks_failed(db, cfg, fake_browser):
    db.add_pending_override(7, "Mystery", "Lead", "aa", "paid")
    override = db.get_pending_override("Mystery", "Lead", "aa", "paid")

    with patch("src.main.overrides_mod") as om:
        main_mod._apply_aa_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    outcomes = db.get_daily_override_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "failed"
    assert "URL not on file" in outcomes[0]["detail"]
    assert db.list_pending_overrides() == []
    om.comment_and_close.assert_called_once()
    # Browser must NOT be touched when there's no URL to navigate to.
    fake_browser.scrape_roles_on_project.assert_not_called()


def test_apply_override_role_not_found_marks_not_found(db, cfg, fake_browser):
    run_id = db.start_run()
    db.record_rejection(
        project_name="Mystery", project_url="https://aa.example/?breakdown=42",
        role_name="Lead", role_description="d", rejection_reason="age",
        run_id=run_id, platform="aa",
    )
    db.add_pending_override(7, "Mystery", "Lead", "aa", "paid")
    override = db.get_pending_override("Mystery", "Lead", "aa", "paid")

    fake_browser.scrape_roles_on_project.return_value = (
        [{"role_name": "Different Role", "role_id": "1", "description": ""}],
        "",
    )

    with patch("src.main.overrides_mod") as om:
        main_mod._apply_aa_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    outcomes = db.get_daily_override_outcomes()
    assert outcomes[0]["outcome"] == "not_found"
    assert db.list_pending_overrides() == []
    fake_browser.submit_for_role.assert_not_called()
    # Rejection row stays — we couldn't apply, so it's still rejected.
    assert db.is_rejected("Lead", "Mystery", "aa") is True


def test_apply_override_happy_path_records_application_and_clears_rejection(db, cfg, fake_browser):
    run_id = db.start_run()
    db.record_rejection(
        project_name="Mystery", project_url="https://aa.example/?breakdown=42",
        role_name="Lead", role_description="prev desc", rejection_reason="age",
        run_id=run_id, platform="aa",
    )
    db.add_pending_override(7, "Mystery", "Lead", "aa", "paid")
    override = db.get_pending_override("Mystery", "Lead", "aa", "paid")

    fake_browser.scrape_roles_on_project.return_value = (
        [{"role_name": "Lead", "role_id": "999", "description": "fresh", "element": object()}],
        "",
    )
    fake_browser.submit_for_role.return_value = True

    with patch("src.main.overrides_mod") as om:
        main_mod._apply_aa_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    # Application recorded with override marker.
    assert db.is_applied("42_999") is True
    apps = db.get_daily_applications()
    assert len(apps) == 1
    assert apps[0]["project_name"] == "Mystery"
    assert "OVERRIDE" in apps[0]["ai_reason"]

    # Rejection cleared so it doesn't keep showing up in Passed.
    assert db.is_rejected("Lead", "Mystery", "aa") is False

    # Outcome recorded.
    outcomes = db.get_daily_override_outcomes()
    assert outcomes[0]["outcome"] == "applied"

    # Pending row cleared.
    assert db.list_pending_overrides() == []

    # default_note must have been blanked out for the submit call.
    submit_call_cfg = fake_browser.submit_for_role.call_args.args[2]
    assert submit_call_cfg["default_note"] == ""


def test_apply_override_submit_failure_marks_failed_keeps_rejection(db, cfg, fake_browser):
    run_id = db.start_run()
    db.record_rejection(
        project_name="P", project_url="https://aa.example/?breakdown=10",
        role_name="R", role_description="d", rejection_reason="age",
        run_id=run_id, platform="aa",
    )
    db.add_pending_override(8, "P", "R", "aa", "paid")
    override = db.get_pending_override("P", "R", "aa", "paid")

    fake_browser.scrape_roles_on_project.return_value = (
        [{"role_name": "R", "role_id": "5", "description": "", "element": object()}],
        "",
    )
    fake_browser.submit_for_role.return_value = False

    with patch("src.main.overrides_mod"):
        main_mod._apply_aa_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    outcomes = db.get_daily_override_outcomes()
    assert outcomes[0]["outcome"] == "failed"
    # Rejection NOT cleared on failure — role still belongs in Passed.
    assert db.is_rejected("R", "P", "aa") is True
    # Pending row cleared regardless (don't infinite-loop).
    assert db.list_pending_overrides() == []


def test_apply_override_dry_run_skips_submit_and_keeps_queue(db, cfg, fake_browser):
    run_id = db.start_run()
    db.record_rejection(
        project_name="P", project_url="https://aa.example/?breakdown=1",
        role_name="R", role_description="d", rejection_reason="age",
        run_id=run_id, platform="aa",
    )
    db.add_pending_override(9, "P", "R", "aa", "paid")
    override = db.get_pending_override("P", "R", "aa", "paid")

    with patch("src.main.overrides_mod"):
        main_mod._apply_aa_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=True,
        )

    fake_browser.scrape_roles_on_project.assert_not_called()
    fake_browser.submit_for_role.assert_not_called()
    # Queue preserved so the real run still picks it up.
    assert len(db.list_pending_overrides()) == 1


# --- process_aa_overrides routing ---

def test_process_aa_overrides_only_processes_aa_for_this_mode(db, cfg, fake_browser):
    db.add_pending_override(1, "P1", "R1", "aa", "paid")
    db.add_pending_override(2, "P2", "R2", "aa", "unpaid")  # different mode
    db.add_pending_override(3, "P3", "R3", "backstage", "paid")  # different platform

    # Pre-load project URLs so the AA paid one can proceed.
    run_id = db.start_run()
    db.record_rejection(
        project_name="P1", project_url="https://aa.example/?breakdown=1",
        role_name="R1", role_description="d", rejection_reason="age",
        run_id=run_id, platform="aa",
    )
    fake_browser.scrape_roles_on_project.return_value = (
        [{"role_name": "R1", "role_id": "10", "description": "", "element": object()}],
        "",
    )
    fake_browser.submit_for_role.return_value = True

    with patch.dict(os.environ, {"OVERRIDE_GITHUB_TOKEN": "tok"}), \
         patch("src.main.overrides_mod") as om:
        om.load_run_config.return_value = (cfg["overrides"], "tok")
        om.fetch_pending_with_errors.return_value = ([], [])
        main_mod.process_aa_overrides(cfg, db, fake_browser, run_id, mode="paid", dry_run=False)

    # Only the AA paid one was applied.
    assert fake_browser.submit_for_role.call_count == 1
    # The other two are still queued.
    remaining = {(o["platform"], o["mode"]) for o in db.list_pending_overrides()}
    assert ("aa", "unpaid") in remaining
    assert ("backstage", "paid") in remaining


def test_process_aa_overrides_noop_without_config_or_token(db, fake_browser):
    """No config + no token → returns immediately, no DB writes, no browser calls."""
    cfg_no = {"submission": {}}
    with patch.dict(os.environ, {}, clear=True), \
         patch("src.main.overrides_mod") as om:
        om.load_run_config.return_value = (None, None)
        main_mod.process_aa_overrides(cfg_no, db, fake_browser, 1, mode="paid", dry_run=False)
    om.fetch_pending_with_errors.assert_not_called()
    fake_browser.scrape_roles_on_project.assert_not_called()
