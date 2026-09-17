"""Rebuild applied.db from the published submissions archive HTML.

Disaster-recovery tool. On 2026-09-17 the `gh release upload --clobber` step
in the unpaid workflow deleted the existing applied.db release asset and then
failed (HTTP 500 from GitHub) before the replacement finished uploading, so the
db-storage release ended up with no database at all. The only complete copy of
the submission history was the archive page on gh-pages, which src/archive.py
renders from `Database.get_all_submission_records()`.

This script inverts that render: it parses every `<tr class="record">` row
back into applied_roles / flagged_roles / rejected_roles rows and writes a
fresh SQLite database with the current schema (created via
`src.database.Database`, so migrations/columns match what the bot expects).

What is recovered exactly: record type (applied/draft/flagged/rejected),
timestamp (to the second), platform, mode, project name, project URL, role
name, role description, AI/flag/rejection reason, submission note or
suggested cover letter.

What cannot be recovered from the archive and is synthesised instead:
  * applied_roles.role_id — the dedup key.
      - cn:        exact  (`cn_{project}_{role}` parsed from the role URL)
      - aa:        `{breakdown_id}_recovered{n}` (breakdown id from the URL,
                   so `has_seen_breakdown()` still works; exact role ids are
                   not in the archive, name-based dedup covers the rest)
      - backstage: `backstage_{project_id}_recovered{n}` (Backstage also
                   dedups server-side via has_submission)
  * run_history, digest_history, shadow_comparisons, pending_overrides,
    override_history — not in the archive. digest_history gets a single
    seed row (see --last-digest) so the next digest does not span history.
  * candidates_considered, info_note, run_id, draft_app_id — left at defaults.

Optionally, `--extra-log` files (GitHub Actions job logs from runs that
happened after the archive was generated) are scanned for
"[DB] Recording application / rejection / flagged" lines and merged in, so
work done between the last archive publish and the data loss is not dropped.

Usage:
    python scripts/rebuild_db_from_archive.py \
        --archive site/index.html --output data/applied.db \
        [--extra-log logs/run108.txt ...] [--last-digest "2026-09-17 20:16:00"]
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import Database  # noqa: E402

TYPE_LABELS = {"APPLIED": "applied", "DRAFT": "draft", "FLAGGED": "flagged", "PASSED": "rejected"}
PLATFORM_LABELS = {"Actors Access": "aa", "Casting Networks": "cn", "Backstage": "backstage"}

_ROW_RE = re.compile(r'<tr class="record">(.*?)</tr>', re.S)
_DATE_RE = re.compile(r'<td class="date">(.*?)</td>', re.S)
_BADGE_RE = re.compile(r'<span class="badge"[^>]*>(.*?)</span>', re.S)
_MODE_RE = re.compile(r'<span class="mode">(.*?)</span>', re.S)
_PROJECT_TD_RE = re.compile(r'<td class="project">(.*?)</td>', re.S)
_PROJECT_LINK_RE = re.compile(r'<a href="(.*?)"[^>]*>(.*?)</a>', re.S)
_ROLE_TD_RE = re.compile(r'<td class="role">(.*)$', re.S)
_ROLENAME_RE = re.compile(r'<div class="rolename">(.*?)</div>', re.S)
_REASON_RE = re.compile(r'<div class="reason"><strong>[^<]*</strong> (.*?)</div>', re.S)
_NOTE_RE = re.compile(r'<div class="note"><strong>[^<]*</strong> (.*?)</div>', re.S)
_DESC_RE = re.compile(r'<div class="desc">(.*?)</div></details>', re.S)

_CN_ID_RE = re.compile(r"/project/(\d+)/role/(\d+)")
_AA_ID_RE = re.compile(r"breakdown=(\d+)")
_BS_ID_RE = re.compile(r"backstage\.com/casting/[^/]*?-(\d+)/?")


def _unescape(s: str | None) -> str:
    return html.unescape(s) if s else ""


def parse_archive(page: str) -> list[dict]:
    records = []
    for m in _ROW_RE.finditer(page):
        row = m.group(1)
        date_iso = _unescape(_DATE_RE.search(row).group(1)).strip()
        badges = _BADGE_RE.findall(row)
        if len(badges) < 2:
            raise ValueError(f"Row without type/platform badges: {row[:200]}")
        record_type = TYPE_LABELS.get(_unescape(badges[0]).strip())
        platform_label = _unescape(badges[1]).strip()
        platform = PLATFORM_LABELS.get(platform_label, platform_label.lower())
        if record_type is None:
            raise ValueError(f"Unknown record type badge: {badges[0]!r}")
        mode_m = _MODE_RE.search(row)
        mode = _unescape(mode_m.group(1)).strip() if mode_m else ""

        proj_td = _PROJECT_TD_RE.search(row).group(1)
        link = _PROJECT_LINK_RE.search(proj_td)
        if link:
            project_url = _unescape(link.group(1))
            project_name = _unescape(link.group(2))
        else:
            project_url = ""
            project_name = _unescape(proj_td)
        if project_name == "(unknown project)":
            project_name = ""

        role_td = _ROLE_TD_RE.search(row).group(1)
        role_name = _unescape(_ROLENAME_RE.search(role_td).group(1))
        if role_name == "(unknown role)":
            role_name = ""
        reason_m = _REASON_RE.search(role_td)
        note_m = _NOTE_RE.search(role_td)
        desc_m = _DESC_RE.search(role_td)

        records.append({
            "record_type": record_type,
            "date_iso": date_iso,
            "platform": platform,
            "mode": mode,
            "project_name": project_name,
            "project_url": project_url,
            "role_name": role_name,
            "role_description": _unescape(desc_m.group(1)) if desc_m else "",
            "reason": _unescape(reason_m.group(1)) if reason_m else "",
            "submission_note": _unescape(note_m.group(1)) if note_m else "",
        })
    return records


# --- extra log ingestion -------------------------------------------------------

# 2026-09-17T20:16:26.3685593Z 2026-09-17 20:16:26,366 [INFO] src.database: [DB] Recording application: Proj — Role (id=..., mode=unpaid, status=submitted)
_LOG_APP_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[INFO\] \S+: \[DB\] Recording application: "
    r"(?P<project>.*?) — (?P<role>.*?) \(id=(?P<role_id>[^,)]+), mode=(?P<mode>\w+), status=(?P<status>\w+)\)\s*$"
)
_LOG_REJ_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[INFO\] \S+: \[DB\] Recording rejection: "
    r"(?P<project>.*?) — (?P<role>.*?) \((?P<reason>.*), mode=(?P<mode>\w+)\)\s*$"
)
_LOG_FLAG_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[INFO\] \S+: \[DB\] Recording flagged role: "
    r"(?P<project>.*?) — (?P<role>.*?) \((?P<reason>.*), mode=(?P<mode>\w+)\)\s*$"
)
_STEP_RE = re.compile(r"##\[group\]Run (?:xvfb-run --auto-servernum )?python -m src\.(?P<mod>main|cn\.main|backstage\.main)")
_STEP_PLATFORM = {"main": "aa", "cn.main": "cn", "backstage.main": "backstage"}


def _infer_platform_from_role_id(role_id: str, fallback: str) -> str:
    if role_id.startswith("cn_"):
        return "cn"
    if role_id.startswith("backstage_"):
        return "backstage"
    return fallback or "aa"


def parse_extra_log(text: str) -> list[dict]:
    """Extract DB write events from a GitHub Actions job log."""
    out = []
    platform = "aa"
    for line in text.splitlines():
        step = _STEP_RE.search(line)
        if step:
            platform = _STEP_PLATFORM[step.group("mod")]
            continue
        m = _LOG_APP_RE.search(line)
        if m:
            out.append({
                "record_type": "draft" if m.group("status") == "draft" else "applied",
                "date_iso": m.group("ts"),
                "platform": _infer_platform_from_role_id(m.group("role_id"), platform),
                "mode": m.group("mode"),
                "project_name": m.group("project"),
                "project_url": "",
                "role_name": m.group("role"),
                "role_description": "",
                "reason": "recovered from run log",
                "submission_note": "",
                "role_id": m.group("role_id"),
            })
            continue
        for rx, kind in ((_LOG_REJ_RE, "rejected"), (_LOG_FLAG_RE, "flagged")):
            m = rx.search(line)
            if m:
                out.append({
                    "record_type": kind,
                    "date_iso": m.group("ts"),
                    "platform": platform,
                    "mode": m.group("mode"),
                    "project_name": m.group("project"),
                    "project_url": "",
                    "role_name": m.group("role"),
                    "role_description": "",
                    "reason": m.group("reason"),
                    "submission_note": "",
                })
                break
    return out


# --- writing -------------------------------------------------------------------

def _synth_role_id(rec: dict, counters: Counter) -> str:
    url = rec["project_url"]
    platform = rec["platform"]
    if platform == "cn":
        m = _CN_ID_RE.search(url)
        if m:
            return f"cn_{m.group(1)}_{m.group(2)}"
        prefix = "cn_unknown"
    elif platform == "backstage":
        m = _BS_ID_RE.search(url)
        prefix = f"backstage_{m.group(1)}" if m else "backstage_unknown"
    else:
        m = _AA_ID_RE.search(url)
        prefix = m.group(1) if m else "aa_unknown"
    counters[prefix] += 1
    return f"{prefix}_recovered{counters[prefix]}"


def build_db(records: list[dict], output: str, last_digest: str | None) -> dict:
    if os.path.exists(output):
        raise SystemExit(f"Refusing to overwrite existing {output}")
    db = Database(output)
    conn = db.conn
    counters: Counter = Counter()
    stats: Counter = Counter()
    # Oldest first so autoincrement ids follow chronology.
    for rec in sorted(records, key=lambda r: r["date_iso"]):
        kind = rec["record_type"]
        if kind in ("applied", "draft"):
            role_id = rec.get("role_id") or _synth_role_id(rec, counters)
            cur = conn.execute(
                """INSERT OR IGNORE INTO applied_roles
                   (role_id, project_name, role_name, role_description, ai_reason,
                    candidates_considered, platform, project_url, applied_at,
                    submission_note, mode, status, info_note)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, '')""",
                (role_id, rec["project_name"], rec["role_name"], rec["role_description"],
                 rec["reason"], rec["platform"], rec["project_url"], rec["date_iso"],
                 rec["submission_note"], rec["mode"] or "paid",
                 "draft" if kind == "draft" else "submitted"),
            )
        elif kind == "flagged":
            cur = conn.execute(
                """INSERT OR IGNORE INTO flagged_roles
                   (project_name, project_url, role_name, role_description, flag_reason,
                    run_id, platform, flagged_at, mode, suggested_note)
                   VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                (rec["project_name"], rec["project_url"], rec["role_name"],
                 rec["role_description"], rec["reason"], rec["platform"],
                 rec["date_iso"], rec["mode"] or "paid", rec["submission_note"]),
            )
        elif kind == "rejected":
            cur = conn.execute(
                """INSERT OR IGNORE INTO rejected_roles
                   (project_name, project_url, role_name, role_description,
                    rejection_reason, run_id, platform, rejected_at, mode)
                   VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                (rec["project_name"], rec["project_url"], rec["role_name"],
                 rec["role_description"], rec["reason"], rec["platform"],
                 rec["date_iso"], rec["mode"] or "paid"),
            )
        else:
            raise ValueError(kind)
        stats[f"{kind}/{rec['platform']}"] += cur.rowcount
        if cur.rowcount == 0:
            stats["duplicates_skipped"] += 1
    if last_digest:
        conn.execute("INSERT INTO digest_history (sent_at) VALUES (?)", (last_digest,))
    conn.commit()
    db.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", required=True, help="archive index.html rendered by src.archive")
    ap.add_argument("--output", required=True, help="path of the SQLite DB to create (must not exist)")
    ap.add_argument("--extra-log", action="append", default=[],
                    help="GitHub Actions job log(s) to mine for DB writes newer than the archive")
    ap.add_argument("--last-digest", default=None,
                    help="seed digest_history with this UTC timestamp ('YYYY-MM-DD HH:MM:SS')")
    ap.add_argument("--min-records", type=int, default=0,
                    help="fail if the archive yields fewer records than this (sanity guard)")
    args = ap.parse_args()

    with open(args.archive, encoding="utf-8") as fh:
        page = fh.read()
    records = parse_archive(page)
    declared = re.search(r'<div class="meta">(\d+) records', page)
    if declared and int(declared.group(1)) != len(records):
        raise SystemExit(
            f"Parsed {len(records)} rows but the page declares {declared.group(1)} records — "
            "parser out of sync with src/archive.py, refusing to continue"
        )
    if len(records) < args.min_records:
        raise SystemExit(f"Only {len(records)} records parsed (< --min-records {args.min_records})")
    print(f"Parsed {len(records)} records from {args.archive}")

    newest = max((r["date_iso"] for r in records), default="")
    extra = []
    for path in args.extra_log:
        with open(path, encoding="utf-8", errors="replace") as fh:
            found = parse_extra_log(fh.read())
        kept = [r for r in found if r["date_iso"] > newest[:19]]
        print(f"{path}: {len(found)} DB events, {len(kept)} newer than archive ({newest[:19]})")
        extra.extend(kept)

    stats = build_db(records + extra, args.output, args.last_digest)
    for key in sorted(stats):
        print(f"  {key}: {stats[key]}")
    print(f"Wrote {args.output} ({os.path.getsize(args.output):,} bytes)")


if __name__ == "__main__":
    main()
