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
