# tests/test_shadow_report.py
"""Tests for the shadow-eval reporting surfaces.

Covers the digest-block snippet and the standalone archive page rendered by
``src.shadow_report``. Uses a temporary SQLite DB seeded via the Database
helper so the schema mirrors production.
"""

from __future__ import annotations

import sqlite3
from html.parser import HTMLParser

import pytest

from src.database import Database
from src.shadow_report import (
    ANTHROPIC_PRICING,
    DEEPSEEK_PRICING,
    DEFAULT_CLAUDE_MODEL,
    render_archive_page,
    render_digest_block,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert(conn: sqlite3.Connection, **kwargs) -> int:
    """Insert a shadow_comparisons row, returning its id.

    All columns are optional except the schema-required ones (platform, mode,
    call_site, prompt_hash, prompt_text). Convenient for sparse fixtures.
    """
    defaults = {
        "platform": "aa",
        "mode": "paid",
        "call_site": "single_fit",
        "prompt_hash": "deadbeef",
        "prompt_text": "test prompt",
    }
    defaults.update(kwargs)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    cur = conn.execute(
        f"INSERT INTO shadow_comparisons ({cols}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def db_path(tmp_path):
    """Fresh DB initialized via Database (which creates shadow_comparisons)."""
    path = str(tmp_path / "test.db")
    db = Database(path)
    db.close()
    return path


# ---------------------------------------------------------------------------
# render_digest_block
# ---------------------------------------------------------------------------


def test_digest_empty_returns_empty_string(db_path):
    """No rows in the table → empty string, so the digest skips the section."""
    assert render_digest_block("paid", db_path=db_path) == ""


def test_digest_empty_when_no_rows_today(db_path):
    """A row from a different mode shouldn't trigger the block."""
    conn = sqlite3.connect(db_path)
    _insert(
        conn,
        mode="unpaid",
        claude_verdict="FIT",
        ds_chat_verdict="FIT",
        chat_matches_claude=1,
        ds_chat_latency_ms=1200,
        ds_chat_input_tokens=100, ds_chat_output_tokens=50,
        ds_reasoner_verdict="FIT",
        reasoner_matches_claude=1,
        ds_reasoner_latency_ms=18000,
        ds_reasoner_input_tokens=120, ds_reasoner_output_tokens=80,
    )
    conn.close()
    # Paid digest should not see the unpaid row.
    assert render_digest_block("paid", db_path=db_path) == ""


def test_digest_renders_match_rate_and_disagreement_count(db_path):
    """Seeded rows produce HTML with the expected stats and disagreement count."""
    conn = sqlite3.connect(db_path)
    # 3 paid rows today. chat agrees on 2/3, reasoner agrees on 1/3.
    # Each row should contribute to cost & latency stats.
    rows = [
        # row 1: chat matches, reasoner doesn't
        dict(
            claude_verdict="FIT",
            ds_chat_verdict="FIT", chat_matches_claude=1,
            ds_chat_latency_ms=1000,
            ds_chat_input_tokens=1_000_000, ds_chat_output_tokens=0,
            ds_reasoner_verdict="SKIP", reasoner_matches_claude=0,
            ds_reasoner_latency_ms=20000,
            ds_reasoner_input_tokens=1_000_000, ds_reasoner_output_tokens=0,
        ),
        # row 2: chat matches, reasoner matches
        dict(
            claude_verdict="FIT",
            ds_chat_verdict="FIT", chat_matches_claude=1,
            ds_chat_latency_ms=1200,
            ds_chat_input_tokens=0, ds_chat_output_tokens=0,
            ds_reasoner_verdict="FIT", reasoner_matches_claude=1,
            ds_reasoner_latency_ms=18000,
            ds_reasoner_input_tokens=0, ds_reasoner_output_tokens=0,
        ),
        # row 3: chat doesn't, reasoner doesn't
        dict(
            claude_verdict="FIT",
            ds_chat_verdict="SKIP", chat_matches_claude=0,
            ds_chat_latency_ms=1400,
            ds_chat_input_tokens=0, ds_chat_output_tokens=0,
            ds_reasoner_verdict="SKIP", reasoner_matches_claude=0,
            ds_reasoner_latency_ms=22000,
            ds_reasoner_input_tokens=0, ds_reasoner_output_tokens=0,
        ),
    ]
    for r in rows:
        _insert(conn, **r)
    conn.close()

    html = render_digest_block("paid", db_path=db_path)
    assert html != ""
    # Match-rate detail strings show numerator/denominator.
    assert "2/3" in html  # chat: 2 matches out of 3 comparisons
    assert "1/3" in html  # reasoner: 1 match out of 3 comparisons
    # Disagreement counter — 2 rows have at least one model disagreeing.
    assert "2 disagreements" in html
    # Median latency for chat: median of [1000, 1200, 1400] = 1200ms = "1.2s".
    assert "1.2s" in html
    # Cost: chat row 1 contributed 1M input tokens at $0.27 → "$0.27".
    expected_chat_cost = DEEPSEEK_PRICING["deepseek-chat"]["input"]
    assert f"${expected_chat_cost:.2f}" in html
    # Should include the model labels.
    assert "deepseek-chat" in html
    assert "deepseek-reasoner" in html
    # Should mention "Shadow eval".
    assert "Shadow eval" in html


def test_digest_singular_disagreement_grammar(db_path):
    """'1 disagreement' (singular) is rendered, not '1 disagreements'."""
    conn = sqlite3.connect(db_path)
    _insert(
        conn,
        claude_verdict="FIT",
        ds_chat_verdict="SKIP", chat_matches_claude=0,
        ds_chat_latency_ms=1000,
        ds_reasoner_verdict="FIT", reasoner_matches_claude=1,
        ds_reasoner_latency_ms=20000,
    )
    conn.close()
    html = render_digest_block("paid", db_path=db_path)
    assert "1 disagreement</strong> awaiting review" in html
    assert "1 disagreements" not in html


def test_digest_includes_claude_row_and_savings_line(db_path):
    """Claude is rendered as the baseline row with cost, plus a savings line."""
    conn = sqlite3.connect(db_path)
    # One paid row today with realistic-ish tokens for all three providers.
    # Claude: 1M in / 1M out → $3 + $15 = $18.
    # ds_chat: 1M in / 1M out → $0.27 + $1.10 = $1.37.
    # ds_reasoner: 1M in / 1M out → $0.55 + $2.19 = $2.74.
    _insert(
        conn,
        claude_verdict="FIT",
        claude_input_tokens=1_000_000, claude_output_tokens=1_000_000,
        claude_latency_ms=2000,
        claude_model="claude-sonnet-4-6",
        ds_chat_verdict="FIT", chat_matches_claude=1,
        ds_chat_latency_ms=1200,
        ds_chat_input_tokens=1_000_000, ds_chat_output_tokens=1_000_000,
        ds_reasoner_verdict="FIT", reasoner_matches_claude=1,
        ds_reasoner_latency_ms=18000,
        ds_reasoner_input_tokens=1_000_000, ds_reasoner_output_tokens=1_000_000,
    )
    conn.close()

    html = render_digest_block("paid", db_path=db_path)
    # Claude row appears with model name and its computed cost.
    assert "claude-sonnet-4-6" in html
    assert "$18.00" in html  # 1M*$3 + 1M*$15
    # Claude is the baseline so the match-rate cell is marked, not a percentage.
    assert "baseline" in html
    # DeepSeek costs still present.
    assert "$1.37" in html
    assert "$2.74" in html
    # Savings line shows percentage delta vs Claude. DeepSeek is cheaper, so
    # the deltas are negative.
    assert "Claude today" in html
    assert "DeepSeek would have cost" in html
    # ds_chat is ~92% cheaper than Claude, ds_reasoner ~85% cheaper.
    assert "-92%" in html
    assert "-85%" in html


def test_digest_savings_line_skipped_when_no_claude_tokens(db_path):
    """If Claude tokens are zero/NULL, no savings line is rendered."""
    conn = sqlite3.connect(db_path)
    _insert(
        conn,
        claude_verdict="FIT",
        # No claude_input_tokens / claude_output_tokens.
        ds_chat_verdict="FIT", chat_matches_claude=1,
        ds_chat_latency_ms=1200,
        ds_chat_input_tokens=1_000_000, ds_chat_output_tokens=0,
        ds_reasoner_verdict="FIT", reasoner_matches_claude=1,
        ds_reasoner_latency_ms=18000,
    )
    conn.close()
    html = render_digest_block("paid", db_path=db_path)
    assert "DeepSeek would have cost" not in html


def test_digest_null_claude_model_falls_back_to_default(db_path):
    """A row without claude_model is priced as DEFAULT_CLAUDE_MODEL."""
    conn = sqlite3.connect(db_path)
    _insert(
        conn,
        claude_verdict="FIT",
        claude_input_tokens=1_000_000, claude_output_tokens=0,
        claude_latency_ms=2000,
        # claude_model intentionally omitted (NULL).
        ds_chat_verdict="FIT", chat_matches_claude=1, ds_chat_latency_ms=1000,
        ds_reasoner_verdict="FIT", reasoner_matches_claude=1,
        ds_reasoner_latency_ms=15000,
    )
    conn.close()
    html = render_digest_block("paid", db_path=db_path)
    # Default model name surfaces in the rendered table.
    assert DEFAULT_CLAUDE_MODEL in html
    # 1M input at $3/M → $3.00 should appear.
    expected = ANTHROPIC_PRICING[DEFAULT_CLAUDE_MODEL]["input"]
    assert f"${expected:.2f}" in html


def test_digest_archive_link_uses_env(db_path, monkeypatch):
    """ARCHIVE_SITE_URL produces a clickable link; otherwise falls back."""
    monkeypatch.setenv("ARCHIVE_SITE_URL", "https://example.com/archive")
    conn = sqlite3.connect(db_path)
    _insert(
        conn,
        claude_verdict="FIT",
        ds_chat_verdict="FIT", chat_matches_claude=1, ds_chat_latency_ms=900,
        ds_reasoner_verdict="FIT", reasoner_matches_claude=1, ds_reasoner_latency_ms=15000,
    )
    conn.close()
    html = render_digest_block("paid", db_path=db_path)
    assert 'href="https://example.com/archive/shadow/"' in html

    monkeypatch.delenv("ARCHIVE_SITE_URL")
    html = render_digest_block("paid", db_path=db_path)
    assert "/shadow/" in html
    assert "<a href" not in html  # fallback is plain text


# ---------------------------------------------------------------------------
# render_archive_page
# ---------------------------------------------------------------------------


class _HTMLValidator(HTMLParser):
    """Minimal HTML parser that flags unbalanced tags. Self-closing/void
    elements are ignored for balance checks.
    """

    _VOID = {
        "meta", "link", "br", "hr", "img", "input", "source", "area", "base",
        "col", "embed", "param", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self._VOID:
            return
        if not self.stack:
            self.errors.append(f"closing </{tag}> with empty stack")
            return
        # Pop until we find a match (tolerate optional close tags like <p>).
        if self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.stack.pop()
            if self.stack:
                self.stack.pop()
        else:
            self.errors.append(f"stray </{tag}>")


def test_archive_page_renders_parseable_html(db_path):
    """The archive page is a complete, parseable HTML document."""
    html = render_archive_page(db_path=db_path)
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    parser = _HTMLValidator()
    parser.feed(html)
    # Allow head leftovers (style) to not match if any; just ensure no errors.
    assert parser.errors == [], parser.errors


def test_archive_page_empty_db_has_empty_messages(db_path):
    """With no rows, the three sections render their 'empty' placeholders."""
    html = render_archive_page(db_path=db_path)
    assert "No shadow comparisons recorded yet" in html
    assert "No disagreements yet" in html
    # Stats section is always present; 0 rows in heading.
    assert "0 rows" in html or "0 comparisons" in html


def test_archive_page_includes_row_per_disagreement_in_order(db_path):
    """Every disagreement gets a card; cards appear in newest-first order."""
    conn = sqlite3.connect(db_path)
    # Insert 4 rows: 3 disagreements, 1 full agreement. Use explicit
    # created_at values so ordering is deterministic.
    _insert(
        conn,
        call_site="single_fit",
        project_name="Alpha", role_name="Hero",
        created_at="2026-05-09 10:00:00",
        claude_verdict="FIT", claude_response="FIT claude",
        ds_chat_verdict="SKIP", ds_chat_response="SKIP chat",
        chat_matches_claude=0,
        ds_reasoner_verdict="FIT", ds_reasoner_response="FIT reasoner",
        reasoner_matches_claude=1,
    )
    _insert(
        conn,
        call_site="single_fit",
        project_name="Bravo", role_name="Villain",
        created_at="2026-05-10 11:00:00",
        claude_verdict="FIT", claude_response="FIT claude",
        ds_chat_verdict="FIT", chat_matches_claude=1,
        ds_reasoner_verdict="SKIP", reasoner_matches_claude=0,
    )
    _insert(
        conn,
        call_site="partial_availability",
        project_name="Charlie", role_name="Sidekick",
        created_at="2026-05-11 12:00:00",
        claude_verdict="PROCEED", claude_response="PROCEED claude",
        ds_chat_verdict="SKIP", chat_matches_claude=0,
        ds_reasoner_verdict="SKIP", reasoner_matches_claude=0,
    )
    # Full agreement — should NOT appear in the disagreement section.
    _insert(
        conn,
        call_site="single_fit",
        project_name="Delta", role_name="Quiet",
        created_at="2026-05-11 13:00:00",
        claude_verdict="FIT",
        ds_chat_verdict="FIT", chat_matches_claude=1,
        ds_reasoner_verdict="FIT", reasoner_matches_claude=1,
    )
    conn.close()

    html = render_archive_page(db_path=db_path)
    # All 3 disagreeing projects appear; agreement-only project Delta is absent
    # from the disagreement queue header even if its data may surface in
    # summary tables — but its role name shouldn't appear in any card body.
    assert "Alpha" in html
    assert "Bravo" in html
    assert "Charlie" in html
    # Delta should not be rendered as a disagreement card.
    # (It's not in the summary either since we look at per-call_site stats by
    # call_site, not by project — so a literal substring check on the
    # project name is safe.)
    assert "Delta" not in html

    # Order: Charlie (most recent) before Bravo before Alpha.
    charlie_idx = html.index("Charlie")
    bravo_idx = html.index("Bravo")
    alpha_idx = html.index("Alpha")
    assert charlie_idx < bravo_idx < alpha_idx, (
        f"expected newest-first ordering: charlie={charlie_idx}, "
        f"bravo={bravo_idx}, alpha={alpha_idx}"
    )

    # Disagreement queue count shows 3.
    assert "Disagreement queue" in html
    assert "(3)" in html

    # Header card metadata includes call_site, mode, platform.
    assert "single_fit" in html
    assert "partial_availability" in html


def test_archive_page_summary_includes_per_call_site_rows(db_path):
    """Section A renders one row per (call_site, model) pair."""
    conn = sqlite3.connect(db_path)
    _insert(
        conn,
        call_site="single_fit",
        claude_verdict="FIT",
        ds_chat_verdict="FIT", chat_matches_claude=1, ds_chat_latency_ms=1000,
        ds_chat_input_tokens=100, ds_chat_output_tokens=50,
        ds_reasoner_verdict="FIT", reasoner_matches_claude=1,
        ds_reasoner_latency_ms=15000,
        ds_reasoner_input_tokens=200, ds_reasoner_output_tokens=80,
    )
    _insert(
        conn,
        call_site="partial_availability",
        claude_verdict="PROCEED",
        ds_chat_verdict="PROCEED", chat_matches_claude=1, ds_chat_latency_ms=900,
        ds_reasoner_verdict="SKIP", reasoner_matches_claude=0,
        ds_reasoner_latency_ms=16000,
    )
    conn.close()
    html = render_archive_page(db_path=db_path)
    # 2 call sites × 2 models = 4 summary rows in section A.
    assert "single_fit" in html
    assert "partial_availability" in html
    assert "deepseek-chat" in html
    assert "deepseek-reasoner" in html
    # Stats section shows totals for all providers.
    assert "Claude" in html
    assert "p50 latency" in html
    assert "p95 latency" in html


def test_archive_page_renders_claude_cost(db_path):
    """The cost & latency totals section shows a non-empty cost for Claude."""
    conn = sqlite3.connect(db_path)
    _insert(
        conn,
        claude_verdict="FIT",
        claude_input_tokens=1_000_000, claude_output_tokens=1_000_000,
        claude_latency_ms=2500,
        claude_model="claude-sonnet-4-6",
        ds_chat_verdict="FIT", chat_matches_claude=1, ds_chat_latency_ms=1000,
        ds_chat_input_tokens=500_000, ds_chat_output_tokens=500_000,
        ds_reasoner_verdict="FIT", reasoner_matches_claude=1,
        ds_reasoner_latency_ms=15000,
        ds_reasoner_input_tokens=500_000, ds_reasoner_output_tokens=500_000,
    )
    conn.close()
    html = render_archive_page(db_path=db_path)
    # Claude row's cost cell is now populated. 1M*$3 + 1M*$15 = $18.00.
    assert "$18.00" in html
    # The old "Claude cost is not estimated here" disclaimer is gone.
    assert "Claude cost is not estimated here" not in html
