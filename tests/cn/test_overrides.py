# tests/cn/test_overrides.py
"""Tests for the Apply Anyway override flow in src/cn/main.py.

Mirrors tests/test_main_overrides.py — same outcome matrix
(applied / not_found / failed / dry-run) adapted to CN's URL-based submit
interface. The CN browser, GitHub API, and config loader are mocked.
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# CN main.py imports playwright transitively via src.cn.browser. Tests don't
# need the real browser, so stub the playwright import surface before loading.
if "playwright" not in sys.modules:
    playwright_pkg = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    for name in ("sync_playwright", "Page", "Browser", "BrowserContext"):
        setattr(sync_api, name, type(name, (), {}))
    playwright_pkg.sync_api = sync_api
    sys.modules["playwright"] = playwright_pkg
    sys.modules["playwright.sync_api"] = sync_api

from src.cn import main as cn_main  # noqa: E402
from src.database import Database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return Database(os.path.join(str(tmp_path), "t.db"))


@pytest.fixture
def cfg():
    return {
        "submission": {"default_note": "should be cleared", "phone": "3104567890"},
        "overrides": {"repo": "o/r", "label": "apply-anyway"},
    }


@pytest.fixture
def fake_browser():
    return MagicMock()


def _seed_cn_url(db, project_name, role_name, role_url):
    """Insert a rejected_roles row so get_known_project_url returns role_url."""
    run_id = db.start_run(platform="cn", mode="paid")
    db.record_rejection(
        project_name=project_name, project_url=role_url,
        role_name=role_name, role_description="d", rejection_reason="ai",
        run_id=run_id, platform="cn",
    )


def test_apply_cn_override_no_role_url_marks_failed(db, cfg, fake_browser):
    db.add_pending_override(1, "Mystery", "Lead", "cn", "paid")
    override = db.get_pending_override("Mystery", "Lead", "cn", "paid")

    with patch("src.cn.main.overrides_mod"):
        cn_main._apply_cn_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    outcomes = db.get_daily_override_outcomes()
    assert outcomes[0]["outcome"] == "failed"
    assert "URL not on file" in outcomes[0]["detail"]
    fake_browser.submit_for_role.assert_not_called()
    assert db.list_pending_overrides() == []


def test_apply_cn_override_unparseable_url_marks_failed(db, cfg, fake_browser):
    _seed_cn_url(db, "P", "R", "https://app.castingnetworks.com/something-else/")
    db.add_pending_override(2, "P", "R", "cn", "paid")
    override = db.get_pending_override("P", "R", "cn", "paid")

    with patch("src.cn.main.overrides_mod"):
        cn_main._apply_cn_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    outcomes = db.get_daily_override_outcomes()
    assert outcomes[0]["outcome"] == "failed"
    assert "Could not parse" in outcomes[0]["detail"]
    fake_browser.submit_for_role.assert_not_called()


def test_apply_cn_override_happy_path_records_application(db, cfg, fake_browser):
    role_url = "https://app.castingnetworks.com/project/100/role/200/details/"
    _seed_cn_url(db, "Acme Pictures", "Hero", role_url)
    db.add_pending_override(3, "Acme Pictures", "Hero", "cn", "paid")
    override = db.get_pending_override("Acme Pictures", "Hero", "cn", "paid")

    fake_browser.submit_for_role.return_value = True

    with patch("src.cn.main.overrides_mod") as om:
        cn_main._apply_cn_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    # Application recorded under cn_{project_id}_{role_id} = cn_100_200.
    assert db.is_applied("cn_100_200") is True
    apps = db.get_daily_applications()
    assert apps[0]["platform"] == "cn"
    assert "OVERRIDE" in apps[0]["ai_reason"]

    # Rejection cleared, outcome recorded, pending cleared, issue closed.
    assert db.is_rejected("Hero", "Acme Pictures", "cn") is False
    assert db.get_daily_override_outcomes()[0]["outcome"] == "applied"
    assert db.list_pending_overrides() == []
    om.comment_and_close.assert_called_once()

    # Note must be blanked out, matching the AA override behavior.
    submit_call = fake_browser.submit_for_role.call_args
    role_arg, sub_cfg_arg = submit_call.args
    assert sub_cfg_arg["default_note"] == ""
    assert role_arg["url"] == role_url
    assert role_arg["role_name"] == "Hero"


def test_apply_cn_override_submit_returns_false_marks_failed(db, cfg, fake_browser):
    role_url = "https://app.castingnetworks.com/project/100/role/200/details/"
    _seed_cn_url(db, "P", "R", role_url)
    db.add_pending_override(4, "P", "R", "cn", "paid")
    override = db.get_pending_override("P", "R", "cn", "paid")

    fake_browser.submit_for_role.return_value = False

    with patch("src.cn.main.overrides_mod"):
        cn_main._apply_cn_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=False,
        )

    outcomes = db.get_daily_override_outcomes()
    assert outcomes[0]["outcome"] == "failed"
    # Rejection preserved on failure so the role still surfaces in Passed.
    assert db.is_rejected("R", "P", "cn") is True
    # Pending cleared so we don't retry forever.
    assert db.list_pending_overrides() == []


def test_apply_cn_override_dry_run_skips_submit(db, cfg, fake_browser):
    role_url = "https://app.castingnetworks.com/project/9/role/9/details/"
    _seed_cn_url(db, "P", "R", role_url)
    db.add_pending_override(5, "P", "R", "cn", "paid")
    override = db.get_pending_override("P", "R", "cn", "paid")

    with patch("src.cn.main.overrides_mod"):
        cn_main._apply_cn_override(
            cfg, db, fake_browser, override,
            cfg["overrides"], "tok", dry_run=True,
        )

    fake_browser.submit_for_role.assert_not_called()
    # Queue preserved so the real run picks it up.
    assert len(db.list_pending_overrides()) == 1


def test_process_cn_overrides_only_processes_cn_for_this_mode(db, cfg, fake_browser):
    role_url = "https://app.castingnetworks.com/project/1/role/2/details/"
    _seed_cn_url(db, "P1", "R1", role_url)
    db.add_pending_override(1, "P1", "R1", "cn", "paid")
    db.add_pending_override(2, "P2", "R2", "cn", "unpaid")
    db.add_pending_override(3, "P3", "R3", "aa", "paid")
    fake_browser.submit_for_role.return_value = True

    with patch.dict(os.environ, {"OVERRIDE_GITHUB_TOKEN": "tok"}), \
         patch("src.cn.main.overrides_mod") as om:
        om.load_run_config.return_value = (cfg["overrides"], "tok")
        om.fetch_pending_with_errors.return_value = ([], [])
        cn_main.process_cn_overrides(
            cfg, db, fake_browser, run_id=99, mode="paid", dry_run=False,
        )

    # Only the CN paid one was submitted.
    assert fake_browser.submit_for_role.call_count == 1
    remaining = {(o["platform"], o["mode"]) for o in db.list_pending_overrides()}
    assert ("cn", "unpaid") in remaining
    assert ("aa", "paid") in remaining
