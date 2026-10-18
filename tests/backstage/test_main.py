"""Tests for src.backstage.main entrypoint behavior."""
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.backstage import main as main_mod
from src.backstage.client import TransientBlockError


def _run_main_with(run_once_side_effect):
    """Invoke main() in --once mode with run_once patched to the given effect."""
    with patch.object(main_mod, "load_backstage_config", return_value={"logging": {}}), \
         patch.object(main_mod, "setup_logging"), \
         patch.object(main_mod, "Database", return_value=MagicMock()), \
         patch.object(main_mod, "run_once", side_effect=run_once_side_effect) as run_once, \
         patch.object(sys, "argv", ["main", "--once"]):
        main_mod.main()
    return run_once


def test_transient_block_exits_cleanly():
    """A transient upstream block must not crash main() — the workflow should
    stay green so downstream steps run and the next scheduled pass retries."""
    # Should NOT raise / SystemExit — a clean return means exit code 0.
    _run_main_with(TransientBlockError("Cloudflare challenge"))


def test_genuine_error_still_propagates():
    """Real failures (bad login, missing unpaid search) must still fail the run."""
    with pytest.raises(RuntimeError):
        _run_main_with(RuntimeError("Backstage login failed"))


from src.database import Database

SAVED = {"id": 7, "name": "acting", "search_params": {"gender": "M"}}


def _run_once_with_client(client, db):
    cfg = {"credentials": {"email": "e", "password": "p"}, "max_pages": 1, "saved_search": "acting"}
    with patch.object(main_mod, "BackstageClient", return_value=client), \
         patch.object(main_mod, "process_backstage_overrides"):
        main_mod.run_once(cfg, db, mode="paid")


def _client(saved_searches, listings):
    client = MagicMock()
    client.login.return_value = True
    client.fetch_saved_searches.return_value = saved_searches
    client.fetch_listings.return_value = listings
    return client


def test_successful_saved_search_fetch_is_cached(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    _run_once_with_client(_client([SAVED], {"items": []}), db)
    assert db.get_cached("backstage_saved_search:acting") is not None


def test_blocked_saved_search_falls_back_to_cache(tmp_path):
    """A Cloudflare block on saved searches uses the last good copy instead
    of skipping the whole Backstage run."""
    db = Database(str(tmp_path / "t.db"))
    _run_once_with_client(_client([SAVED], {"items": []}), db)

    client = _client(None, {"items": []})
    _run_once_with_client(client, db)
    assert client.fetch_listings.call_args.kwargs["saved_search"] == SAVED


def test_blocked_without_cache_is_transient(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    with pytest.raises(TransientBlockError):
        _run_once_with_client(_client(None, {"items": []}), db)


def test_cached_search_still_blocked_is_transient(tmp_path):
    """If listings are blocked too, don't record a 'success' that saw nothing."""
    db = Database(str(tmp_path / "t.db"))
    _run_once_with_client(_client([SAVED], {"items": []}), db)
    with pytest.raises(TransientBlockError):
        _run_once_with_client(
            _client(None, {"_error": True, "code": 403, "cloudflare": True}), db
        )


# --- Cover-letter-required roles: auto-submit with a simple letter ---


def test_build_cover_letter_default_is_short_and_names_the_role():
    letter = main_mod.build_cover_letter({"role_name": "Cody"}, {})
    assert "Cody" in letter
    assert "self-tape" in letter
    assert len(letter) < 400
    assert "[" not in letter and "{" not in letter


def test_build_cover_letter_falls_back_to_generic_for_long_role_names():
    long_name = "Tomorrow, Oct 8: Street-Style Portfolio Shoot on Ventura Blvd (TFP)"
    letter = main_mod.build_cover_letter({"role_name": long_name}, {})
    assert long_name not in letter
    assert "this role" in letter


def test_build_cover_letter_config_override_with_role_placeholder():
    cfg = {"submission": {"cover_letter": "Hi! Interested in {role}. Thanks."}}
    assert main_mod.build_cover_letter({"role_name": "Jack"}, cfg) == "Hi! Interested in Jack. Thanks."


def test_finalize_pending_drafts_submits_and_clears_flag(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.record_application("backstage_1", "'Consent'", "Cody", platform="backstage",
                          mode="paid", status="draft", submission_note="old AI letter")
    db.record_flagged_role("'Consent'", "https://www.backstage.com/x/", "Cody", "desc",
                           "Cover letter required (Headshot/Photo, Cover Letter) — draft ready in Backstage",
                           run_id=1, platform="backstage", mode="paid",
                           suggested_note="old AI letter", draft_app_id=4242)
    client = MagicMock()
    client.finalize_draft.return_value = {"id": 4242, "applicant_status": "C"}

    submitted = main_mod.finalize_pending_drafts({}, db, client, mode="paid", dry_run=False)

    assert submitted == 1
    client.finalize_draft.assert_called_once()
    assert client.finalize_draft.call_args.args[0] == 4242
    assert "Cody" in client.finalize_draft.call_args.kwargs["note"]
    assert client.finalize_draft.call_args.kwargs["cover_letter_required"] is True
    assert db.get_pending_backstage_drafts("paid") == []
    row = db.conn.execute(
        "SELECT status, submission_note FROM applied_roles WHERE role_id='backstage_1'"
    ).fetchone()
    assert row[0] == "submitted"
    assert "Cody" in row[1]


def test_finalize_pending_drafts_rejection_keeps_flag_and_stops_retrying(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.record_flagged_role("KO", "https://www.backstage.com/x/", "Cut Man", "desc",
                           "Cover letter required (Video Reel, Cover Letter) — draft ready in Backstage",
                           run_id=1, platform="backstage", mode="paid", draft_app_id=9)
    client = MagicMock()
    client.finalize_draft.return_value = {"_rejected": True, "reason": "Not all role requirements met: Cover Letter", "app_id": 9}

    assert main_mod.finalize_pending_drafts({}, db, client, mode="paid", dry_run=False) == 0
    flagged = db.get_daily_flagged(mode="paid")
    assert len(flagged) == 1
    assert "auto-submit rejected" in flagged[0]["flag_reason"]
    assert flagged[0]["draft_app_id"] == 9
    # Not retried on the next run.
    assert db.get_pending_backstage_drafts("paid") == []


def test_finalize_pending_drafts_dry_run_touches_nothing(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.record_flagged_role("KO", "u", "Cut Man", "desc",
                           "Cover letter required (Cover Letter) — draft ready in Backstage",
                           run_id=1, platform="backstage", mode="paid", draft_app_id=9)
    client = MagicMock()
    assert main_mod.finalize_pending_drafts({}, db, client, mode="paid", dry_run=True) == 0
    client.finalize_draft.assert_not_called()
    assert len(db.get_pending_backstage_drafts("paid")) == 1
