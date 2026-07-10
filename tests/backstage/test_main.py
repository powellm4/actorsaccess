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
