# src/shadow_report.py
"""Renders shadow-eval reporting surfaces.

Two entry points:

- ``render_digest_block(mode) -> str`` — small HTML block injected at the top
  of the daily digest email. Shows today's match rate, median latency, and
  estimated cost per model (Claude baseline + each DeepSeek shadow), plus a
  one-line savings comparison and a count of disagreements awaiting review.

- ``render_archive_page() -> str`` — full HTML document published to
  ``/shadow/index.html`` on the GitHub Pages archive site. Three sections:
  (A) per-call_site agreement summary, (B) disagreement queue with full
  side-by-side prompts and responses, (C) cost/latency totals.

This module is read-only: it queries ``shadow_comparisons`` but never writes
to it. Phase 3 will wire ``render_digest_block`` into ``src/digest.py`` and
the archive page into the publish-archive workflow.

CLI (mirrors ``src/archive.py``):

    python -m src.shadow_report --db data/applied.db --output dist/

writes ``dist/shadow/index.html``.

Design spec: docs/superpowers/specs/2026-05-11-deepseek-shadow-eval-design.md
"""

from __future__ import annotations

import argparse
import html
import os
import sqlite3
import statistics
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Pricing — USD per 1M tokens
# ---------------------------------------------------------------------------
# DeepSeek source: platform.deepseek.com pricing as of 2026-05.
# Anthropic source: anthropic.com/pricing as of 2026-05.
# If pricing changes, bump these and re-run the report.
DEEPSEEK_PRICING = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
}

ANTHROPIC_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}

# Used when a row's claude_model is NULL (rows written before the column
# existed). Matches the production default in shadow.shadowed_completion.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

_ALL_PRICING: dict[str, dict] = {**DEEPSEEK_PRICING, **ANTHROPIC_PRICING}


# Mapping from our internal column prefix to the DeepSeek model name used
# for pricing lookups.
_PREFIX_TO_MODEL = {
    "ds_chat": "deepseek-chat",
    "ds_reasoner": "deepseek-reasoner",
}

_PREFIX_TO_LABEL = {
    "ds_chat": "deepseek-chat",
    "ds_reasoner": "deepseek-reasoner",
}

_PREFIX_TO_MATCH_COL = {
    "ds_chat": "chat_matches_claude",
    "ds_reasoner": "reasoner_matches_claude",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Return USD cost for a given token usage at the listed price.

    Looks up the model in the combined DeepSeek + Anthropic pricing table.
    Returns 0.0 for unknown models so the report still renders cleanly.
    """
    rates = _ALL_PRICING.get(model)
    if not rates:
        return 0.0
    return (input_tokens / 1_000_000.0) * rates["input"] + (
        output_tokens / 1_000_000.0
    ) * rates["output"]


def _median(values: list[int | float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return float(statistics.median(vals))


def _percentile(values: list[int | float], pct: float) -> float | None:
    """Return the ``pct`` percentile (0..100) using nearest-rank."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, int(round((pct / 100.0) * (len(vals) - 1)))))
    return float(vals[k])


def _format_ms(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms)}ms"


def _format_cost(usd: float) -> str:
    if usd >= 1:
        return f"${usd:,.2f}"
    if usd >= 0.01:
        return f"${usd:.2f}"
    return f"${usd:.4f}"


def _format_pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "—"
    return f"{(numerator / denominator) * 100:.0f}%"


def _archive_link() -> str | None:
    """Return absolute URL to the shadow archive page, or None if unset."""
    site_url = (os.environ.get("ARCHIVE_SITE_URL") or "").strip()
    if not site_url:
        return None
    return site_url.rstrip("/") + "/shadow/"


# ---------------------------------------------------------------------------
# Digest block
# ---------------------------------------------------------------------------


def _last_digest_sent(conn: sqlite3.Connection) -> str | None:
    """Return the timestamp of the most recent digest_history row, or None.

    Used to scope the "this digest" cost window. Returns None if the table
    is missing (shadow may be enabled before digest_history is initialized)
    or empty.
    """
    try:
        row = conn.execute(
            "SELECT sent_at FROM digest_history ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _aggregate_for_today(
    conn: sqlite3.Connection, mode: str, since: str | None = None
) -> dict[str, dict] | None:
    """Compute per-model stats for shadow rows created today (UTC).

    When ``since`` is provided, restrict to rows with ``created_at > since``
    (a UTC timestamp string). Otherwise return today's full UTC-date window.

    Returns None if no rows match.
    """
    # SQLite's CURRENT_TIMESTAMP / created_at is UTC. Filter via date('now')
    # to get rows whose created_at is on today's UTC date.
    if since is None:
        cur = conn.execute(
            """
            SELECT
                claude_latency_ms,   claude_input_tokens,   claude_output_tokens,
                claude_model,
                ds_chat_latency_ms,  ds_chat_input_tokens,  ds_chat_output_tokens,
                ds_chat_error,       chat_matches_claude,
                ds_reasoner_latency_ms, ds_reasoner_input_tokens, ds_reasoner_output_tokens,
                ds_reasoner_error,   reasoner_matches_claude
            FROM shadow_comparisons
            WHERE mode = ?
              AND date(created_at) = date('now')
            """,
            (mode,),
        )
    else:
        cur = conn.execute(
            """
            SELECT
                claude_latency_ms,   claude_input_tokens,   claude_output_tokens,
                claude_model,
                ds_chat_latency_ms,  ds_chat_input_tokens,  ds_chat_output_tokens,
                ds_chat_error,       chat_matches_claude,
                ds_reasoner_latency_ms, ds_reasoner_input_tokens, ds_reasoner_output_tokens,
                ds_reasoner_error,   reasoner_matches_claude
            FROM shadow_comparisons
            WHERE mode = ?
              AND created_at > ?
            """,
            (mode, since),
        )
    rows = cur.fetchall()
    if not rows:
        return None

    per_model: dict[str, dict] = {}
    for prefix in ("ds_chat", "ds_reasoner"):
        per_model[prefix] = {
            "latencies": [],
            "cost_usd": 0.0,
            "matches": 0,
            "compared": 0,  # rows where match column is NOT NULL
            "errors": 0,
        }

    # Claude rows grouped by model name (NULL → DEFAULT_CLAUDE_MODEL) so a
    # day with mixed Claude models renders one row per model.
    claude_by_model: dict[str, dict] = {}

    disagreements = 0

    for r in rows:
        (
            cl_lat, cl_in, cl_out, cl_model,
            chat_lat, chat_in, chat_out, chat_err, chat_match,
            reas_lat, reas_in, reas_out, reas_err, reas_match,
        ) = r

        # claude
        model_name = cl_model or DEFAULT_CLAUDE_MODEL
        bucket = claude_by_model.setdefault(model_name, {
            "latencies": [],
            "cost_usd": 0.0,
        })
        if cl_lat is not None:
            bucket["latencies"].append(cl_lat)
        if cl_in or cl_out:
            bucket["cost_usd"] += _estimate_cost(
                cl_in or 0, cl_out or 0, model_name
            )

        # ds_chat
        if chat_lat is not None:
            per_model["ds_chat"]["latencies"].append(chat_lat)
        if chat_in or chat_out:
            per_model["ds_chat"]["cost_usd"] += _estimate_cost(
                chat_in or 0, chat_out or 0, "deepseek-chat"
            )
        if chat_err:
            per_model["ds_chat"]["errors"] += 1
        if chat_match is not None:
            per_model["ds_chat"]["compared"] += 1
            if chat_match == 1:
                per_model["ds_chat"]["matches"] += 1

        # ds_reasoner
        if reas_lat is not None:
            per_model["ds_reasoner"]["latencies"].append(reas_lat)
        if reas_in or reas_out:
            per_model["ds_reasoner"]["cost_usd"] += _estimate_cost(
                reas_in or 0, reas_out or 0, "deepseek-reasoner"
            )
        if reas_err:
            per_model["ds_reasoner"]["errors"] += 1
        if reas_match is not None:
            per_model["ds_reasoner"]["compared"] += 1
            if reas_match == 1:
                per_model["ds_reasoner"]["matches"] += 1

        # A disagreement is a row where either model produced a verdict that
        # didn't match Claude's. NULLs (no verdict, free-form, or error) don't
        # count.
        if chat_match == 0 or reas_match == 0:
            disagreements += 1

    return {
        "rows": len(rows),
        "disagreements": disagreements,
        "per_model": per_model,
        "claude_by_model": claude_by_model,
    }


def render_digest_block(mode: str, db_path: str | None = None) -> str:
    """Return an HTML snippet for the daily digest email.

    Filters shadow_comparisons by ``mode`` (``"paid"`` or ``"unpaid"``) and
    computes per-model match rate, median latency, and estimated cost. Cost
    is shown for the "this digest" window (rows added since the previous
    digest_history entry) with the daily total as a small secondary line;
    when there is no prior digest the two collapse to a single number.
    Returns an empty string when no shadow rows exist for today.

    ``db_path`` is optional; when omitted, falls back to the ``SHADOW_DB_PATH``
    env var, then to ``data/applied.db``.
    """
    db_path = db_path or os.environ.get("SHADOW_DB_PATH") or "data/applied.db"
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return ""
    try:
        try:
            today_agg = _aggregate_for_today(conn, mode)
            last_sent = _last_digest_sent(conn)
            # "This digest" = rows added since the previous digest was sent.
            # When there's no prior digest (first send, or fresh DB) the two
            # windows are identical — fall back to today's aggregation so the
            # cost cell still has a number.
            window_agg = (
                _aggregate_for_today(conn, mode, since=last_sent)
                if last_sent else today_agg
            )
        except sqlite3.OperationalError:
            # Table missing (shadow never enabled on this DB).
            return ""
    finally:
        conn.close()

    if not today_agg:
        return ""

    # When the window query returned nothing (e.g. this digest fires but no new
    # shadow rows landed since the last one), fall back so the table still
    # renders today's numbers rather than going blank.
    show_window = window_agg is not None and last_sent is not None
    if window_agg is None:
        window_agg = today_agg

    def _cost_cell(window_cost: float, today_cost: float) -> str:
        primary = _format_cost(window_cost)
        if not show_window or abs(window_cost - today_cost) < 1e-9:
            return primary
        return (
            f'{primary}'
            f'<div style="color:#90a4ae;font-size:11px;">'
            f'today {_format_cost(today_cost)}</div>'
        )

    rows_html = []
    # Claude rows first (one per distinct claude_model used today). Claude is
    # the baseline so the match-rate cell is "—".
    claude_window_cost = 0.0
    claude_today_cost = 0.0
    claude_models = sorted(
        set(today_agg["claude_by_model"].keys())
        | set(window_agg["claude_by_model"].keys())
    )
    for model_name in claude_models:
        w = window_agg["claude_by_model"].get(model_name, {"latencies": [], "cost_usd": 0.0})
        t = today_agg["claude_by_model"].get(model_name, {"latencies": [], "cost_usd": 0.0})
        claude_window_cost += w["cost_usd"]
        claude_today_cost += t["cost_usd"]
        # Latency uses the window if available, else today — same fallback
        # rule as the cost cell.
        median_lat = _format_ms(_median(w["latencies"] or t["latencies"]))
        rows_html.append(
            f'<tr>'
            f'<td style="padding:4px 8px;font-family:ui-monospace,monospace;">'
            f'{html.escape(model_name)}</td>'
            f'<td style="padding:4px 8px;color:#666;">— (baseline)</td>'
            f'<td style="padding:4px 8px;">{median_lat}</td>'
            f'<td style="padding:4px 8px;">{_cost_cell(w["cost_usd"], t["cost_usd"])}</td>'
            f'</tr>'
        )

    for prefix in ("ds_chat", "ds_reasoner"):
        w = window_agg["per_model"][prefix]
        t = today_agg["per_model"][prefix]
        label = _PREFIX_TO_LABEL[prefix]
        match_rate = _format_pct(w["matches"], w["compared"])
        match_detail = f'{w["matches"]}/{w["compared"]}'
        median_lat = _format_ms(_median(w["latencies"] or t["latencies"]))
        errors_cell = f' <span style="color:#b71c1c;">{w["errors"]} err</span>' if w["errors"] else ""
        rows_html.append(
            f'<tr>'
            f'<td style="padding:4px 8px;font-family:ui-monospace,monospace;">{label}</td>'
            f'<td style="padding:4px 8px;"><strong>{match_rate}</strong> '
            f'<span style="color:#666;font-size:12px;">({match_detail})</span></td>'
            f'<td style="padding:4px 8px;">{median_lat}</td>'
            f'<td style="padding:4px 8px;">{_cost_cell(w["cost_usd"], t["cost_usd"])}{errors_cell}</td>'
            f'</tr>'
        )

    # Savings line: anchored to this-digest cost so the percentages reflect
    # what THIS email represents, not a running daily total that drifts as
    # successive digests fire through the day.
    savings_html = ""
    if claude_window_cost > 0:
        deltas = []
        for prefix in ("ds_chat", "ds_reasoner"):
            ds_cost = window_agg["per_model"][prefix]["cost_usd"]
            if ds_cost <= 0:
                continue
            pct = (ds_cost - claude_window_cost) / claude_window_cost * 100
            sign = "+" if pct >= 0 else ""
            deltas.append(
                f'{_PREFIX_TO_LABEL[prefix]} {_format_cost(ds_cost)} '
                f'({sign}{pct:.0f}%)'
            )
        if deltas:
            today_suffix = (
                f' <span style="color:#90a4ae;">'
                f'(today {_format_cost(claude_today_cost)})</span>'
                if show_window and abs(claude_today_cost - claude_window_cost) > 1e-9
                else ""
            )
            savings_html = (
                f'<p style="margin:8px 0 0 0;color:#37474f;font-size:13px;">'
                f'Claude this digest: <strong>{_format_cost(claude_window_cost)}</strong>'
                f'{today_suffix}'
                f' — DeepSeek would have cost: {", ".join(deltas)}'
                f'</p>'
            )

    link = _archive_link()
    if link:
        link_html = (
            f'<a href="{html.escape(link, quote=True)}" '
            'style="color:#1565c0;text-decoration:underline;">view in archive</a>'
        )
    else:
        link_html = 'see <code>/shadow/</code> on the archive site'

    disagreement_html = (
        f'<strong>{today_agg["disagreements"]} disagreement'
        f'{"" if today_agg["disagreements"] == 1 else "s"}</strong> awaiting review'
    )

    return (
        '<div class="shadow-eval" style="margin:0 0 16px 0;padding:14px 16px;'
        'background:#f3f6fa;border-left:4px solid #455a64;border-radius:4px;'
        'color:#263238;font-size:14px;">'
        '<h3 style="margin:0 0 8px 0;font-size:15px;color:#37474f;">'
        'Shadow eval (this digest)</h3>'
        '<table style="border-collapse:collapse;font-size:13px;">'
        '<thead><tr style="color:#546e7a;">'
        '<th style="text-align:left;padding:2px 8px;">model</th>'
        '<th style="text-align:left;padding:2px 8px;">match rate</th>'
        '<th style="text-align:left;padding:2px 8px;">median latency</th>'
        '<th style="text-align:left;padding:2px 8px;">est. cost</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody>'
        '</table>'
        f'{savings_html}'
        f'<p style="margin:8px 0 0 0;">{disagreement_html} — {link_html}</p>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Archive page
# ---------------------------------------------------------------------------


def _fetch_summary_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return aggregated stats per (call_site, model) for the summary table."""
    cur = conn.execute(
        """
        SELECT call_site,
               ds_chat_latency_ms, ds_chat_input_tokens, ds_chat_output_tokens,
               chat_matches_claude,
               ds_reasoner_latency_ms, ds_reasoner_input_tokens, ds_reasoner_output_tokens,
               reasoner_matches_claude,
               created_at
        FROM shadow_comparisons
        """
    )
    rows = cur.fetchall()
    cols: dict[tuple[str, str], dict] = {}

    def _bucket(call_site: str, prefix: str) -> dict:
        key = (call_site, prefix)
        if key not in cols:
            cols[key] = {
                "call_site": call_site,
                "prefix": prefix,
                "rows": 0,
                "matches_all": 0,
                "compared_all": 0,
                "matches_7d": 0,
                "compared_7d": 0,
                "latencies": [],
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            }
        return cols[key]

    for r in rows:
        (
            call_site,
            chat_lat, chat_in, chat_out, chat_match,
            reas_lat, reas_in, reas_out, reas_match,
            created_at,
        ) = r
        is_7d = _is_within_last_7_days(created_at)

        for prefix, lat, in_tok, out_tok, match in (
            ("ds_chat", chat_lat, chat_in, chat_out, chat_match),
            ("ds_reasoner", reas_lat, reas_in, reas_out, reas_match),
        ):
            b = _bucket(call_site, prefix)
            b["rows"] += 1
            if lat is not None:
                b["latencies"].append(lat)
            if in_tok:
                b["input_tokens"] += in_tok
            if out_tok:
                b["output_tokens"] += out_tok
            if in_tok or out_tok:
                b["cost_usd"] += _estimate_cost(
                    in_tok or 0, out_tok or 0, _PREFIX_TO_MODEL[prefix]
                )
            if match is not None:
                b["compared_all"] += 1
                if match == 1:
                    b["matches_all"] += 1
                if is_7d:
                    b["compared_7d"] += 1
                    if match == 1:
                        b["matches_7d"] += 1

    # Stable ordering: by call_site then prefix.
    return sorted(
        cols.values(),
        key=lambda b: (b["call_site"], b["prefix"]),
    )


def _is_within_last_7_days(created_at: str | None) -> bool:
    if not created_at:
        return False
    # SQLite stores either "YYYY-MM-DD HH:MM:SS" (default) or with fractional
    # seconds. Parse leniently.
    try:
        dt = datetime.fromisoformat(created_at.replace(" ", "T"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(tz=timezone.utc) - dt
    return delta.total_seconds() <= 7 * 86400


def _fetch_disagreements(conn: sqlite3.Connection) -> list[dict]:
    """Return all rows where at least one model disagreed with Claude.

    Newest first.
    """
    cur = conn.execute(
        """
        SELECT id, call_site, mode, platform, project_name, role_name,
               created_at, prompt_text,
               claude_response, claude_verdict, claude_latency_ms,
               claude_input_tokens, claude_output_tokens,
               ds_chat_response, ds_chat_verdict, ds_chat_latency_ms,
               ds_chat_input_tokens, ds_chat_output_tokens, ds_chat_error,
               chat_matches_claude,
               ds_reasoner_response, ds_reasoner_verdict, ds_reasoner_latency_ms,
               ds_reasoner_input_tokens, ds_reasoner_output_tokens, ds_reasoner_error,
               reasoner_matches_claude
        FROM shadow_comparisons
        WHERE chat_matches_claude = 0 OR reasoner_matches_claude = 0
        ORDER BY created_at DESC, id DESC
        """
    )
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _fetch_overall_stats(conn: sqlite3.Connection) -> dict:
    """Totals + p50/p95 latency for the cost & latency section."""
    cur = conn.execute(
        """
        SELECT claude_latency_ms, claude_input_tokens, claude_output_tokens,
               claude_model,
               ds_chat_latency_ms, ds_chat_input_tokens, ds_chat_output_tokens,
               ds_reasoner_latency_ms, ds_reasoner_input_tokens, ds_reasoner_output_tokens
        FROM shadow_comparisons
        """
    )
    rows = cur.fetchall()

    stats = {
        "claude": {"latencies": [], "input": 0, "output": 0, "cost": 0.0},
        "ds_chat": {"latencies": [], "input": 0, "output": 0, "cost": 0.0},
        "ds_reasoner": {"latencies": [], "input": 0, "output": 0, "cost": 0.0},
        "row_count": len(rows),
    }
    for r in rows:
        (cl, ci, co, cl_model, chl, chi, cho, rl, ri, ro) = r
        if cl is not None:
            stats["claude"]["latencies"].append(cl)
        stats["claude"]["input"] += ci or 0
        stats["claude"]["output"] += co or 0
        stats["claude"]["cost"] += _estimate_cost(
            ci or 0, co or 0, cl_model or DEFAULT_CLAUDE_MODEL
        )
        if chl is not None:
            stats["ds_chat"]["latencies"].append(chl)
        stats["ds_chat"]["input"] += chi or 0
        stats["ds_chat"]["output"] += cho or 0
        stats["ds_chat"]["cost"] += _estimate_cost(
            chi or 0, cho or 0, "deepseek-chat"
        )
        if rl is not None:
            stats["ds_reasoner"]["latencies"].append(rl)
        stats["ds_reasoner"]["input"] += ri or 0
        stats["ds_reasoner"]["output"] += ro or 0
        stats["ds_reasoner"]["cost"] += _estimate_cost(
            ri or 0, ro or 0, "deepseek-reasoner"
        )
    return stats


def _render_summary_section(rows: list[dict]) -> str:
    if not rows:
        return (
            '<section><h2>Per call-site agreement</h2>'
            '<p class="empty">No shadow comparisons recorded yet.</p></section>'
        )
    body_rows = []
    for b in rows:
        match_all = _format_pct(b["matches_all"], b["compared_all"])
        match_all_detail = f'{b["matches_all"]}/{b["compared_all"]}'
        match_7d = _format_pct(b["matches_7d"], b["compared_7d"])
        match_7d_detail = f'{b["matches_7d"]}/{b["compared_7d"]}'
        body_rows.append(
            '<tr>'
            f'<td>{html.escape(b["call_site"])}</td>'
            f'<td><code>{html.escape(_PREFIX_TO_LABEL[b["prefix"]])}</code></td>'
            f'<td><strong>{match_all}</strong> '
            f'<span class="muted">({match_all_detail})</span></td>'
            f'<td><strong>{match_7d}</strong> '
            f'<span class="muted">({match_7d_detail})</span></td>'
            f'<td>{b["rows"]}</td>'
            f'<td>{_format_ms(_median(b["latencies"]))}</td>'
            f'<td>{b["input_tokens"] + b["output_tokens"]:,}</td>'
            f'<td>{_format_cost(b["cost_usd"])}</td>'
            '</tr>'
        )
    return (
        '<section>'
        '<h2>Per call-site agreement</h2>'
        '<table class="summary">'
        '<thead><tr>'
        '<th>Call site</th><th>Model</th>'
        '<th>Agreement (all-time)</th><th>Agreement (7d)</th>'
        '<th>Rows</th><th>Median latency</th><th>Total tokens</th><th>Est. cost</th>'
        '</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody>'
        '</table>'
        '</section>'
    )


def _render_disagreement_card(row: dict) -> str:
    """Render one disagreement as a collapsible <details> card."""
    call_site = html.escape(row.get("call_site") or "?")
    mode = html.escape(row.get("mode") or "")
    platform = html.escape(row.get("platform") or "")
    project = html.escape(row.get("project_name") or "")
    role = html.escape(row.get("role_name") or "")
    created_at = html.escape((row.get("created_at") or "")[:19])

    claude_verdict = row.get("claude_verdict")
    chat_verdict = row.get("ds_chat_verdict")
    reas_verdict = row.get("ds_reasoner_verdict")

    chat_match = row.get("chat_matches_claude")
    reas_match = row.get("reasoner_matches_claude")

    header_meta = " &middot; ".join(
        x for x in (
            f'<code>{call_site}</code>',
            f'<span class="chip">{mode}</span>' if mode else "",
            f'<span class="chip">{platform}</span>' if platform else "",
            project,
            role,
            f'<span class="muted">{created_at}</span>' if created_at else "",
        )
        if x
    )

    summary_line = f'Claude: <strong>{html.escape(claude_verdict or "—")}</strong>'
    if chat_verdict is not None or chat_match is not None:
        cls = "disagree" if chat_match == 0 else "agree"
        summary_line += (
            f' &middot; chat: <span class="{cls}">'
            f'{html.escape(chat_verdict or "—")}</span>'
        )
    if reas_verdict is not None or reas_match is not None:
        cls = "disagree" if reas_match == 0 else "agree"
        summary_line += (
            f' &middot; reasoner: <span class="{cls}">'
            f'{html.escape(reas_verdict or "—")}</span>'
        )

    prompt_text = html.escape(row.get("prompt_text") or "")

    def _model_cell(
        label: str, prefix: str,
        verdict: str | None, response: str | None, error: str | None,
        latency: int | None, in_tok: int | None, out_tok: int | None,
        match: int | None,
    ) -> str:
        is_disagree = match == 0
        cell_cls = "model-cell disagree" if is_disagree else "model-cell"
        verdict_html = (
            f'<div class="verdict">Verdict: <strong>{html.escape(verdict or "—")}</strong></div>'
        )
        error_html = (
            f'<div class="error">Error: {html.escape(error)}</div>' if error else ""
        )
        meta_bits = []
        if latency is not None:
            meta_bits.append(_format_ms(latency))
        if in_tok is not None or out_tok is not None:
            meta_bits.append(f'{(in_tok or 0) + (out_tok or 0):,} tok')
        meta_html = (
            f'<div class="muted small">{" &middot; ".join(meta_bits)}</div>'
            if meta_bits else ""
        )
        response_html = (
            f'<pre>{html.escape(response or "")}</pre>' if response else
            '<pre class="muted">(no response)</pre>'
        )
        return (
            f'<div class="{cell_cls}">'
            f'<h4>{html.escape(label)}</h4>'
            f'{verdict_html}{error_html}{meta_html}{response_html}'
            '</div>'
        )

    claude_cell = _model_cell(
        "Claude", "claude",
        claude_verdict,
        row.get("claude_response"),
        None,
        row.get("claude_latency_ms"),
        row.get("claude_input_tokens"),
        row.get("claude_output_tokens"),
        match=None,
    )
    chat_cell = _model_cell(
        "DeepSeek-chat", "ds_chat",
        chat_verdict,
        row.get("ds_chat_response"),
        row.get("ds_chat_error"),
        row.get("ds_chat_latency_ms"),
        row.get("ds_chat_input_tokens"),
        row.get("ds_chat_output_tokens"),
        match=chat_match,
    )
    reas_cell = _model_cell(
        "DeepSeek-reasoner", "ds_reasoner",
        reas_verdict,
        row.get("ds_reasoner_response"),
        row.get("ds_reasoner_error"),
        row.get("ds_reasoner_latency_ms"),
        row.get("ds_reasoner_input_tokens"),
        row.get("ds_reasoner_output_tokens"),
        match=reas_match,
    )

    return (
        f'<details class="disagreement" data-id="{row.get("id")}">'
        f'<summary><div class="dis-header">{header_meta}</div>'
        f'<div class="dis-summary">{summary_line}</div></summary>'
        '<div class="dis-body">'
        '<h4>Prompt</h4>'
        f'<pre class="prompt">{prompt_text}</pre>'
        '<div class="model-grid">'
        f'{claude_cell}{chat_cell}{reas_cell}'
        '</div>'
        '</div>'
        '</details>'
    )


def _render_disagreements_section(rows: list[dict]) -> str:
    if not rows:
        return (
            '<section><h2>Disagreement queue</h2>'
            '<p class="empty">No disagreements yet — every comparison agreed.</p></section>'
        )
    cards = "\n".join(_render_disagreement_card(r) for r in rows)
    return (
        '<section>'
        f'<h2>Disagreement queue <span class="muted">({len(rows)})</span></h2>'
        '<p class="muted small">Newest first. Click a row to expand the prompt and side-by-side responses.</p>'
        f'{cards}'
        '</section>'
    )


def _render_stats_section(stats: dict) -> str:
    rows_html = []
    for label, key in (
        ("Claude", "claude"),
        ("DeepSeek-chat", "ds_chat"),
        ("DeepSeek-reasoner", "ds_reasoner"),
    ):
        s = stats[key]
        total_tokens = s["input"] + s["output"]
        cost_cell = _format_cost(s["cost"])
        rows_html.append(
            '<tr>'
            f'<td>{html.escape(label)}</td>'
            f'<td>{s["input"]:,}</td>'
            f'<td>{s["output"]:,}</td>'
            f'<td>{total_tokens:,}</td>'
            f'<td>{_format_ms(_percentile(s["latencies"], 50))}</td>'
            f'<td>{_format_ms(_percentile(s["latencies"], 95))}</td>'
            f'<td>{cost_cell}</td>'
            '</tr>'
        )
    return (
        '<section>'
        f'<h2>Cost &amp; latency totals <span class="muted">({stats["row_count"]} rows)</span></h2>'
        '<p class="muted small">Cost is estimated using each provider\'s published '
        'per-token pricing (see <code>DEEPSEEK_PRICING</code> and '
        '<code>ANTHROPIC_PRICING</code> in <code>shadow_report.py</code>). '
        'Rows missing <code>claude_model</code> are priced as '
        f'<code>{DEFAULT_CLAUDE_MODEL}</code>.</p>'
        '<table class="summary">'
        '<thead><tr>'
        '<th>Provider</th><th>Input tokens</th><th>Output tokens</th>'
        '<th>Total tokens</th><th>p50 latency</th><th>p95 latency</th>'
        '<th>Est. cost</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody>'
        '</table>'
        '</section>'
    )


def render_archive_page(db_path: str | None = None) -> str:
    """Render the full HTML document for /shadow/index.html.

    ``db_path`` lets the CLI and tests pass an explicit path; production uses
    ``data/applied.db`` by default.
    """
    db_path = db_path or os.environ.get("SHADOW_DB_PATH") or "data/applied.db"
    conn = sqlite3.connect(db_path)
    try:
        try:
            summary_rows = _fetch_summary_rows(conn)
            disagreements = _fetch_disagreements(conn)
            stats = _fetch_overall_stats(conn)
        except sqlite3.OperationalError:
            summary_rows, disagreements = [], []
            stats = {
                "claude": {"latencies": [], "input": 0, "output": 0, "cost": 0.0},
                "ds_chat": {"latencies": [], "input": 0, "output": 0, "cost": 0.0},
                "ds_reasoner": {"latencies": [], "input": 0, "output": 0, "cost": 0.0},
                "row_count": 0,
            }
    finally:
        conn.close()

    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    summary_html = _render_summary_section(summary_rows)
    disagreements_html = _render_disagreements_section(disagreements)
    stats_html = _render_stats_section(stats)

    archive_link = _archive_link()
    main_link_html = (
        f'<a href="{html.escape(archive_link[:-len("/shadow/")] or "/", quote=True)}">'
        '&larr; submissions archive</a>'
        if archive_link else
        '<a href="../">&larr; submissions archive</a>'
    )

    return _ARCHIVE_PAGE_TEMPLATE.format(
        generated_at=html.escape(generated_at),
        total=stats["row_count"],
        main_link=main_link_html,
        summary=summary_html,
        disagreements=disagreements_html,
        stats=stats_html,
    )


_ARCHIVE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shadow Eval &mdash; DeepSeek vs Claude</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 16px; background: #fafafa; color: #222; }}
  header {{ border-bottom: 1px solid #ddd; padding-bottom: 12px; margin-bottom: 16px; }}
  header h1 {{ margin: 0 0 4px 0; font-size: 22px; }}
  header .meta {{ color: #666; font-size: 13px; }}
  header a {{ color: #1565c0; text-decoration: none; font-size: 13px; }}
  header a:hover {{ text-decoration: underline; }}
  section {{ margin-bottom: 32px; }}
  h2 {{ font-size: 17px; margin: 0 0 8px 0; }}
  h4 {{ margin: 0 0 4px 0; font-size: 13px; color: #455a64; }}
  .muted {{ color: #888; }}
  .small {{ font-size: 12px; }}
  .empty {{ color: #888; font-style: italic; }}
  code {{ font-family: ui-monospace, "SF Mono", Menlo, monospace;
         background: #eef; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
  .chip {{ display: inline-block; background: #cfd8dc; color: #263238;
           padding: 1px 6px; border-radius: 10px; font-size: 11px;
           margin: 0 4px; }}
  table.summary {{ width: 100%; border-collapse: collapse; background: #fff;
                   border: 1px solid #e0e0e0; border-radius: 6px; overflow: hidden; }}
  table.summary th, table.summary td {{ text-align: left; padding: 8px 10px;
                                        border-bottom: 1px solid #eee; font-size: 13px;
                                        vertical-align: top; }}
  table.summary th {{ background: #f0f0f0; font-size: 12px; text-transform: uppercase;
                      color: #555; }}
  details.disagreement {{ background: #fff; border: 1px solid #e0e0e0;
                          border-radius: 6px; margin-bottom: 8px; padding: 10px 12px; }}
  details.disagreement summary {{ cursor: pointer; list-style: none; }}
  details.disagreement summary::-webkit-details-marker {{ display: none; }}
  details.disagreement .dis-header {{ font-size: 13px; color: #455a64;
                                       margin-bottom: 4px; }}
  details.disagreement .dis-summary {{ font-size: 14px; }}
  details.disagreement[open] {{ background: #fcfcfc; }}
  .dis-body {{ margin-top: 12px; border-top: 1px solid #eee; padding-top: 10px; }}
  .prompt {{ background: #f5f5f5; padding: 10px; border-radius: 4px;
             font-size: 12px; max-height: 240px; overflow: auto;
             white-space: pre-wrap; word-break: break-word; }}
  .model-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px;
                 margin-top: 10px; }}
  .model-cell {{ background: #fafafa; border: 1px solid #e0e0e0;
                 border-radius: 4px; padding: 8px; font-size: 12px; }}
  .model-cell.disagree {{ background: #fff3e0; border-color: #ffb74d; }}
  .model-cell pre {{ background: #fff; border: 1px solid #eee; padding: 6px;
                     max-height: 220px; overflow: auto; white-space: pre-wrap;
                     word-break: break-word; font-size: 12px; margin: 6px 0 0 0; }}
  .verdict {{ font-size: 12px; }}
  .error {{ color: #b71c1c; font-size: 12px; margin-top: 4px; }}
  .disagree {{ color: #b71c1c; font-weight: 600; }}
  .agree {{ color: #2e7d32; font-weight: 600; }}
  @media (max-width: 700px) {{
    .model-grid {{ grid-template-columns: 1fr; }}
    table.summary, table.summary thead, table.summary tbody,
    table.summary tr, table.summary th, table.summary td {{ display: block; }}
    table.summary thead {{ display: none; }}
    table.summary tr {{ border-bottom: 1px solid #eee; padding: 8px 0; }}
    table.summary td {{ border: none; padding: 2px 0; }}
  }}
</style>
</head>
<body>
<header>
  <h1>Shadow Eval &mdash; DeepSeek vs Claude</h1>
  <div class="meta">{total} comparisons recorded &middot; generated {generated_at}</div>
  <div>{main_link}</div>
</header>
{summary}
{disagreements}
{stats}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render the shadow-eval archive page to a directory."
    )
    parser.add_argument("--db", required=True, help="Path to the SQLite database.")
    parser.add_argument(
        "--output", required=True,
        help="Output directory. Writes <DIR>/shadow/index.html.",
    )
    args = parser.parse_args()

    page = render_archive_page(db_path=args.db)
    out_dir = os.path.join(args.output, "shadow")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {len(page):,} bytes to {out_path}")


if __name__ == "__main__":
    main()
