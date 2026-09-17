"""Round-trip test for scripts/rebuild_db_from_archive.py: render an archive page
from known records with src.archive, parse it back, and check that everything
the archive carries survives, plus that the synthesised dedup keys behave."""
import importlib.util
import pathlib
import sqlite3

from src.archive import render_archive_html

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rebuild_db_from_archive.py"
_spec = importlib.util.spec_from_file_location("rebuild_db_from_archive", _SCRIPT)
rebuild = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rebuild)

RECORDS = [
    {
        "record_type": "applied", "date_iso": "2026-09-10 18:02:11.123456", "platform": "aa",
        "mode": "paid", "project_name": "THE <BIG> ONE & MORE",
        "project_url": "https://actorsaccess.com/projects/?view=breakdowns&breakdown=901234&region=5",
        "role_name": "MAX \"THE HAMMER\"", "role_description": "Line one\nLine two <b>",
        "reason": "Strong fit; it's 'quoted'", "submission_note": "Hi — note",
    },
    {
        "record_type": "draft", "date_iso": "2026-09-11 01:00:00", "platform": "backstage",
        "mode": "unpaid", "project_name": "Student Short",
        "project_url": "https://www.backstage.com/casting/student-short-3282351/",
        "role_name": "Calvin", "role_description": "", "reason": "", "submission_note": "",
    },
    {
        "record_type": "applied", "date_iso": "2026-09-12 12:00:00", "platform": "cn",
        "mode": "paid", "project_name": "CN Project",
        "project_url": "https://app.castingnetworks.com/talent/project/16058812/role/67470974",
        "role_name": "Jesse", "role_description": "desc", "reason": "why", "submission_note": "",
    },
    {
        "record_type": "flagged", "date_iso": "2026-09-13 09:30:00", "platform": "cn",
        "mode": "paid", "project_name": "Flag Me", "project_url": "",
        "role_name": "Guy", "role_description": "d", "reason": "Needs a cover letter",
        "submission_note": "Dear casting, ...",
    },
    {
        "record_type": "rejected", "date_iso": "2026-09-14 09:30:00", "platform": "aa",
        "mode": "unpaid", "project_name": "", "project_url": "",
        "role_name": "", "role_description": "", "reason": "Not a romance", "submission_note": "",
    },
]


def test_round_trip(tmp_path):
    page = render_archive_html(RECORDS, "2026-09-17 19:16 UTC")
    parsed = rebuild.parse_archive(page)
    assert len(parsed) == len(RECORDS)
    by_key = {(r["record_type"], r["date_iso"][:19]): r for r in parsed}
    for orig in RECORDS:
        got = by_key[(orig["record_type"], orig["date_iso"][:19])]
        for col in ("platform", "mode", "project_name", "project_url", "role_name",
                    "role_description", "reason", "submission_note"):
            assert got[col] == orig[col], col

    out = tmp_path / "rebuilt.db"
    stats = rebuild.build_db(parsed, str(out), last_digest="2026-09-17 20:16:00.000000")
    assert stats["applied/aa"] == 1 and stats["draft/backstage"] == 1 and stats["applied/cn"] == 1
    assert stats["flagged/cn"] == 1 and stats["rejected/aa"] == 1

    conn = sqlite3.connect(out)
    ids = dict(conn.execute("SELECT platform, role_id FROM applied_roles").fetchall())
    assert ids["cn"] == "cn_16058812_67470974"            # exact, parsed from the URL
    assert ids["aa"].startswith("901234_")                # breakdown prefix keeps has_seen_breakdown working
    assert ids["backstage"].startswith("backstage_3282351_")
    assert conn.execute("SELECT status FROM applied_roles WHERE platform='backstage'").fetchone()[0] == "draft"
    assert conn.execute("SELECT suggested_note FROM flagged_roles").fetchone()[0] == "Dear casting, ..."
    assert conn.execute("SELECT sent_at FROM digest_history").fetchone()[0] == "2026-09-17 20:16:00.000000"

    # The rebuilt DB renders to the same number of archive rows.
    from src.database import Database
    db = Database(str(out))
    assert len(db.get_all_submission_records()) == len(RECORDS)
    assert db.is_applied("cn_16058812_67470974")
    assert db.has_seen_breakdown("901234", "aa", "paid")
    assert db.is_flagged("Guy", "Flag Me", "cn")


def test_declared_count_guard_is_available():
    page = render_archive_html(RECORDS, "now")
    assert f"{len(RECORDS)} records" in page


def test_parse_extra_log_platform_sections():
    log = "\n".join([
        "2026-09-17T19:37:36Z ##[group]Run python -m src.main --once --mode unpaid \\",
        "2026-09-17T19:38:33Z 2026-09-17 19:38:33,275 [INFO] src.database: [DB] Recording application: DEVIL'S GATE — JETT (id=906065_5366096, mode=unpaid, status=submitted)",
        "2026-09-17T19:46:59Z ##[group]Run xvfb-run --auto-servernum python -m src.cn.main --once --mode unpaid \\",
        "2026-09-17T19:47:24Z 2026-09-17 19:47:24,810 [INFO] src.database: [DB] Recording rejection: Boxed In — THE UNMADE (role-type unclear (see rule), mode=unpaid)",
        "2026-09-17T20:14:56Z ##[group]Run python -m src.backstage.main --once --mode unpaid \\",
        "2026-09-17T20:15:18Z 2026-09-17 20:15:18,889 [INFO] src.database: [DB] Recording application: Thesis Film. — Calvin (id=backstage_3282351_5630861, mode=unpaid, status=draft)",
        "2026-09-17T20:15:19Z 2026-09-17 20:15:19,000 [INFO] src.database: [DB] Recording flagged role: Thesis Film. — Tyler (needs cover letter, mode=unpaid)",
    ])
    events = rebuild.parse_extra_log(log)
    kinds = [(e["record_type"], e["platform"], e["mode"]) for e in events]
    assert kinds == [
        ("applied", "aa", "unpaid"),
        ("rejected", "cn", "unpaid"),
        ("draft", "backstage", "unpaid"),
        ("flagged", "backstage", "unpaid"),
    ]
    assert events[0]["role_id"] == "906065_5366096"
    assert events[1]["reason"] == "role-type unclear (see rule)"
    assert events[1]["date_iso"] == "2026-09-17 19:47:24"
