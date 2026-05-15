# tests/test_shadow_wrapper.py
"""Integration tests for src.shadow.shadowed_completion.

NOTE: paused during the Claude API → Claude CLI migration. The wrapper
previously called `claude_client.messages.create()` (mocked here via
FakeClaude) and fired DeepSeek shadow comparisons. With the migration:
  - Claude calls now go through `subprocess.run(["claude", ...])`.
  - `_shadow_enabled()` is hard-coded to False so no DeepSeek work runs.

These tests are kept as documentation for the shadow-eval contract that
should still hold once shadow is re-enabled, and skipped in the meantime
so they don't try to call the real CLI.
"""

import os
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from src import shadow
from src.database import Database
from src.shadow import (
    VERDICT_EXTRACTORS,
    flush_pending_shadows,
    shadowed_completion,
)

pytestmark = pytest.mark.skip(
    reason="Shadow eval paused during Claude API → CLI migration; "
    "re-enable when shadow.py is reactivated."
)


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


class FakeClaude:
    """Stand-in for anthropic.Anthropic that returns a canned text response."""

    def __init__(self, text: str, input_tokens: int = 42, output_tokens: int = 17):
        self._text = text
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, *, model, max_tokens, messages):
        return SimpleNamespace(
            content=[SimpleNamespace(text=self._text)],
            usage=SimpleNamespace(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            ),
        )


@pytest.fixture
def shadow_db(tmp_path, monkeypatch):
    """Initialise a fresh sqlite DB with the shadow_comparisons table and
    point shadow.py at it via set_db_path()."""
    db_path = str(tmp_path / "test_shadow.db")
    # Creating Database() runs _create_tables() which now includes the
    # shadow_comparisons table.
    db = Database(db_path)
    db.close()
    shadow.set_db_path(db_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("SHADOW_ENABLED", raising=False)
    yield db_path
    # Reset module-level state so subsequent tests don't share connections.
    shadow.set_db_path("data/applied.db")


def _fetch_row(db_path, row_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT * FROM shadow_comparisons WHERE id = ?", (row_id,)
        )
        return dict(cur.fetchone())
    finally:
        conn.close()


def _fetch_only_row(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM shadow_comparisons")
        rows = cur.fetchall()
        assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
        return dict(rows[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_returns_claude_text_verbatim(shadow_db, monkeypatch):
    """Caller must get Claude's text exactly as returned."""
    monkeypatch.setattr(
        shadow, "call_deepseek",
        lambda model, prompt, max_tokens, api_key: ("FIT - ds reply", 11, 5),
    )
    claude = FakeClaude("FIT - claude says fit\n")
    out = shadowed_completion(
        "prompt body",
        call_site="single_fit",
        max_tokens=500,
        claude_client=claude,
        extract_verdict=VERDICT_EXTRACTORS["single_fit"],
        platform="aa",
        mode="paid",
        project_name="Test",
        role_name="Hero",
    )
    assert out == "FIT - claude says fit\n"


def test_partial_row_inserted_immediately(shadow_db, monkeypatch):
    """The claude_* half of the row should be present even before flush."""
    barrier = threading.Event()

    def slow_ds(model, prompt, max_tokens, api_key):
        # Hold DeepSeek until the test releases — proves the row was inserted
        # while DeepSeek was still running.
        barrier.wait(timeout=5)
        return ("FIT - ds", 1, 1)

    monkeypatch.setattr(shadow, "call_deepseek", slow_ds)
    claude = FakeClaude("FIT - claude")
    shadowed_completion(
        "p",
        call_site="single_fit",
        max_tokens=100,
        claude_client=claude,
        extract_verdict=VERDICT_EXTRACTORS["single_fit"],
        platform="aa",
        mode="paid",
        project_name="P",
        role_name="R",
    )
    # Inspect DB before DeepSeek has finished.
    row = _fetch_only_row(shadow_db)
    assert row["claude_response"] == "FIT - claude"
    assert row["claude_verdict"] == "FIT"
    assert row["claude_input_tokens"] == 42
    assert row["claude_output_tokens"] == 17
    assert row["claude_latency_ms"] is not None
    # Default model name persists so reports can price historical rows.
    assert row["claude_model"] == "claude-sonnet-4-6"
    # DeepSeek half not populated yet.
    assert row["ds_chat_response"] is None
    assert row["ds_reasoner_response"] is None
    # Now release and drain.
    barrier.set()
    flush_pending_shadows()


def test_explicit_claude_model_recorded(shadow_db, monkeypatch):
    """Passing a non-default claude_model is recorded on the row."""
    monkeypatch.setattr(
        shadow, "call_deepseek",
        lambda model, prompt, max_tokens, api_key: ("FIT", 1, 1),
    )
    claude = FakeClaude("FIT - claude")
    shadowed_completion(
        "p",
        call_site="single_fit",
        max_tokens=100,
        claude_client=claude,
        claude_model="claude-opus-4-7",
        extract_verdict=VERDICT_EXTRACTORS["single_fit"],
        platform="aa",
        mode="paid",
    )
    flush_pending_shadows()
    row = _fetch_only_row(shadow_db)
    assert row["claude_model"] == "claude-opus-4-7"


def test_deepseek_updates_after_flush(shadow_db, monkeypatch):
    """After flush, both DeepSeek halves should be populated with matches."""
    def fake_ds(model, prompt, max_tokens, api_key):
        # Return different text per model so we can distinguish columns.
        if model == "deepseek-chat":
            return ("FIT - chat agrees", 100, 50)
        return ("SKIP - reasoner disagrees", 200, 80)

    monkeypatch.setattr(shadow, "call_deepseek", fake_ds)
    claude = FakeClaude("FIT - claude says fit")
    shadowed_completion(
        "p",
        call_site="single_fit",
        max_tokens=100,
        claude_client=claude,
        extract_verdict=VERDICT_EXTRACTORS["single_fit"],
        platform="aa",
        mode="paid",
    )
    flush_pending_shadows()
    row = _fetch_only_row(shadow_db)
    assert row["claude_verdict"] == "FIT"
    assert row["ds_chat_response"] == "FIT - chat agrees"
    assert row["ds_chat_verdict"] == "FIT"
    assert row["ds_chat_input_tokens"] == 100
    assert row["ds_chat_output_tokens"] == 50
    assert row["ds_chat_error"] is None
    assert row["chat_matches_claude"] == 1
    assert row["ds_reasoner_response"] == "SKIP - reasoner disagrees"
    assert row["ds_reasoner_verdict"] == "SKIP"
    assert row["ds_reasoner_error"] is None
    assert row["reasoner_matches_claude"] == 0


def test_deepseek_failure_recorded_as_error(shadow_db, monkeypatch):
    """When DeepSeek raises, ds_*_error is populated and the call still returns Claude's text."""
    def boom(model, prompt, max_tokens, api_key):
        raise TimeoutError("simulated network timeout")

    monkeypatch.setattr(shadow, "call_deepseek", boom)
    claude = FakeClaude("FIT - claude")
    out = shadowed_completion(
        "p",
        call_site="single_fit",
        max_tokens=100,
        claude_client=claude,
        extract_verdict=VERDICT_EXTRACTORS["single_fit"],
        platform="aa",
        mode="paid",
    )
    assert out == "FIT - claude"  # caller unaffected
    flush_pending_shadows()
    row = _fetch_only_row(shadow_db)
    assert row["claude_verdict"] == "FIT"
    assert row["ds_chat_error"] is not None
    assert "simulated network timeout" in row["ds_chat_error"]
    assert row["ds_chat_response"] is None
    assert row["ds_chat_verdict"] is None
    assert row["chat_matches_claude"] is None
    assert row["ds_reasoner_error"] is not None
    assert row["reasoner_matches_claude"] is None


def test_kill_switch_no_api_key(shadow_db, monkeypatch):
    """If DEEPSEEK_API_KEY is unset, no shadow row is written and no DS call is made."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(
        shadow, "call_deepseek",
        lambda *a, **k: called.append(1) or ("x", 1, 1),
    )
    claude = FakeClaude("FIT - claude")
    out = shadowed_completion(
        "p",
        call_site="single_fit",
        max_tokens=100,
        claude_client=claude,
        extract_verdict=VERDICT_EXTRACTORS["single_fit"],
        platform="aa",
        mode="paid",
    )
    assert out == "FIT - claude"
    assert called == []  # no DS call attempted
    conn = sqlite3.connect(shadow_db)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM shadow_comparisons")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_kill_switch_shadow_disabled(shadow_db, monkeypatch):
    """SHADOW_ENABLED=0 disables shadow even with a key set."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("SHADOW_ENABLED", "0")
    called = []
    monkeypatch.setattr(
        shadow, "call_deepseek",
        lambda *a, **k: called.append(1) or ("x", 1, 1),
    )
    claude = FakeClaude("FIT - claude")
    shadowed_completion(
        "p",
        call_site="single_fit",
        max_tokens=100,
        claude_client=claude,
        extract_verdict=VERDICT_EXTRACTORS["single_fit"],
        platform="aa",
        mode="paid",
    )
    assert called == []
    conn = sqlite3.connect(shadow_db)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM shadow_comparisons")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_cover_letter_no_verdict_comparison(shadow_db, monkeypatch):
    """For cover_letter (no extractor verdict), match columns stay NULL."""
    monkeypatch.setattr(
        shadow, "call_deepseek",
        lambda *a, **k: ("some ds cover letter prose", 50, 80),
    )
    claude = FakeClaude("Claude's free-form cover letter prose.")
    shadowed_completion(
        "p",
        call_site="cover_letter",
        max_tokens=400,
        claude_client=claude,
        extract_verdict=VERDICT_EXTRACTORS["cover_letter"],
        platform="backstage",
        mode="paid",
    )
    flush_pending_shadows()
    row = _fetch_only_row(shadow_db)
    assert row["claude_response"] == "Claude's free-form cover letter prose."
    assert row["claude_verdict"] is None
    assert row["ds_chat_response"] == "some ds cover letter prose"
    # No verdict, no match flag.
    assert row["chat_matches_claude"] is None
    assert row["reasoner_matches_claude"] is None
