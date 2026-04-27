# tests/test_archive.py
"""Tests for the searchable submissions archive renderer."""

from src.archive import render_archive_html


def test_render_empty():
    """An empty archive renders a valid HTML doc with a 'no records' message."""
    html = render_archive_html([], generated_at="2026-04-27 12:00 UTC")
    assert "<!DOCTYPE html>" in html
    assert "Submissions Archive" in html
    assert "No submission records yet" in html
    assert "0 records" in html


def test_render_includes_all_record_types():
    """Each record_type renders with its distinct badge label."""
    records = [
        {"record_type": "applied", "date_iso": "2026-04-27 10:00:00", "platform": "aa",
         "project_name": "P1", "role_name": "R1", "role_description": "", "reason": "",
         "submission_note": "", "mode": "paid", "project_url": ""},
        {"record_type": "draft", "date_iso": "2026-04-26 10:00:00", "platform": "backstage",
         "project_name": "P2", "role_name": "R2", "role_description": "", "reason": "",
         "submission_note": "", "mode": "paid", "project_url": ""},
        {"record_type": "flagged", "date_iso": "2026-04-25 10:00:00", "platform": "cn",
         "project_name": "P3", "role_name": "R3", "role_description": "", "reason": "Needs reel",
         "submission_note": "", "mode": "unpaid", "project_url": ""},
        {"record_type": "rejected", "date_iso": "2026-04-24 10:00:00", "platform": "aa",
         "project_name": "P4", "role_name": "R4", "role_description": "", "reason": "Age",
         "submission_note": "", "mode": "paid", "project_url": ""},
    ]
    html = render_archive_html(records, generated_at="now")
    for badge in ("APPLIED", "DRAFT", "FLAGGED", "PASSED"):
        assert badge in html
    for role in ("R1", "R2", "R3", "R4"):
        assert role in html
    assert "4 records" in html


def test_render_escapes_html_in_user_content():
    """Breakdown text containing HTML must be escaped, not executed."""
    records = [
        {
            "record_type": "applied", "date_iso": "2026-04-27 10:00:00", "platform": "aa",
            "project_name": "<b>Bad</b> Project", "role_name": "<i>Role</i>",
            "role_description": "<script>alert(1)</script>",
            "reason": "<img src=x onerror=alert(1)>", "submission_note": "",
            "mode": "paid", "project_url": "https://example.com/?x=<y>",
        }
    ]
    html = render_archive_html(records, generated_at="now")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;Bad&lt;/b&gt; Project" in html
    assert "<img src=x onerror=alert(1)>" not in html


def test_records_render_in_db_order():
    """Renderer preserves the order it received — the DB query supplies date-desc."""
    records = [
        {"record_type": "applied", "date_iso": "2026-04-27 10:00:00", "platform": "aa",
         "project_name": "Newer", "role_name": "Newer Role", "role_description": "",
         "reason": "", "submission_note": "", "mode": "paid", "project_url": ""},
        {"record_type": "applied", "date_iso": "2026-04-20 10:00:00", "platform": "aa",
         "project_name": "Older", "role_name": "Older Role", "role_description": "",
         "reason": "", "submission_note": "", "mode": "paid", "project_url": ""},
    ]
    html = render_archive_html(records, generated_at="now")
    assert html.index("Newer Role") < html.index("Older Role")


def test_render_includes_search_input_and_script():
    """The page must contain the live-filter input and JS so search works offline."""
    html = render_archive_html([], generated_at="now")
    assert 'id="search"' in html
    assert "addEventListener" in html
    assert "classList" in html


def test_render_links_project_when_url_present():
    records = [
        {"record_type": "applied", "date_iso": "2026-04-27 10:00:00", "platform": "aa",
         "project_name": "Linkable", "role_name": "Lead",
         "role_description": "", "reason": "", "submission_note": "", "mode": "paid",
         "project_url": "https://actorsaccess.com/projects/?breakdown=999"},
    ]
    html = render_archive_html(records, generated_at="now")
    assert 'href="https://actorsaccess.com/projects/?breakdown=999"' in html
    assert ">Linkable</a>" in html


def test_render_role_description_is_searchable():
    """Description must be in the DOM (inside <details>) so the live filter
    matches against it even before the user expands the row."""
    records = [
        {"record_type": "applied", "date_iso": "2026-04-27 10:00:00", "platform": "aa",
         "project_name": "Religious Family Drama", "role_name": "JORDAN",
         "role_description": "20 to 24 years old; Petaluma, CA shoot",
         "reason": "Best match", "submission_note": "", "mode": "paid", "project_url": ""},
    ]
    html = render_archive_html(records, generated_at="now")
    assert "JORDAN" in html
    assert "Petaluma, CA" in html
    assert "Best match" in html
