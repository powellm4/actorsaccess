# tests/test_database.py
import os
import pytest
from src.database import Database


@pytest.fixture
def db(tmp_path):
    db_path = os.path.join(str(tmp_path), "test.db")
    return Database(db_path)


def test_tables_created(db):
    """DB should create applied_roles and run_history tables on init."""
    cursor = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert "applied_roles" in tables
    assert "run_history" in tables


def test_is_applied_false(db):
    assert db.is_applied("role_123") is False


def test_record_application(db):
    db.record_application("role_123", "Test Project", "Lead Role")
    assert db.is_applied("role_123") is True


def test_record_application_duplicate(db):
    db.record_application("role_123", "Test Project", "Lead Role")
    # Second call should not raise
    db.record_application("role_123", "Test Project", "Lead Role")
    # Should still show as applied
    assert db.is_applied("role_123") is True


def test_start_and_complete_run(db):
    run_id = db.start_run()
    assert run_id is not None
    db.complete_run(run_id, roles_found=10, roles_applied=3, roles_skipped=7)
    cursor = db.conn.execute(
        "SELECT status, roles_applied FROM run_history WHERE id = ?",
        (run_id,),
    )
    row = cursor.fetchone()
    assert row[0] == "success"
    assert row[1] == 3


def test_fail_run(db):
    run_id = db.start_run()
    db.fail_run(run_id, "Login failed")
    cursor = db.conn.execute(
        "SELECT status, error_message FROM run_history WHERE id = ?",
        (run_id,),
    )
    row = cursor.fetchone()
    assert row[0] == "error"
    assert row[1] == "Login failed"


def test_record_application_default_platform(db):
    """Recording with no platform arg should default to 'aa'."""
    db.record_application("role_aa", "AA Project", "AA Role")
    cursor = db.conn.execute(
        "SELECT platform FROM applied_roles WHERE role_id = ?", ("role_aa",)
    )
    row = cursor.fetchone()
    assert row[0] == "aa"


def test_record_application_cn_platform(db):
    """Recording with platform='cn' should store 'cn'."""
    db.record_application("role_cn", "CN Project", "CN Role", platform="cn")
    cursor = db.conn.execute(
        "SELECT platform FROM applied_roles WHERE role_id = ?", ("role_cn",)
    )
    row = cursor.fetchone()
    assert row[0] == "cn"


def test_is_applied_respects_platform(db):
    """is_applied should find a CN role by role_id."""
    db.record_application("role_cn2", "CN Project", "CN Role", platform="cn")
    assert db.is_applied("role_cn2") is True


def test_start_run_with_platform(db):
    """start_run with platform='cn' should store 'cn'."""
    run_id = db.start_run(platform="cn")
    cursor = db.conn.execute(
        "SELECT platform FROM run_history WHERE id = ?", (run_id,)
    )
    row = cursor.fetchone()
    assert row[0] == "cn"


def test_start_run_default_platform(db):
    """start_run with no platform arg should default to 'aa'."""
    run_id = db.start_run()
    cursor = db.conn.execute(
        "SELECT platform FROM run_history WHERE id = ?", (run_id,)
    )
    row = cursor.fetchone()
    assert row[0] == "aa"


def test_rejected_roles_table_created(db):
    """DB should create rejected_roles table on init."""
    cursor = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert "rejected_roles" in tables


def test_record_rejection(db):
    run_id = db.start_run()
    db.record_rejection(
        project_name="Test Project",
        project_url="https://actorsaccess.com/projects/?breakdown=123",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Age range too high",
        run_id=run_id,
        platform="aa",
    )
    cursor = db.conn.execute(
        "SELECT project_name, role_name, rejection_reason, platform FROM rejected_roles"
    )
    row = cursor.fetchone()
    assert row[0] == "Test Project"
    assert row[1] == "Villain"
    assert row[2] == "Age range too high"
    assert row[3] == "aa"


def test_record_rejection_upserts(db):
    """Second rejection for same role/project/platform should update reason."""
    run_id = db.start_run()
    db.record_rejection(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Age range too high",
        run_id=run_id,
        platform="aa",
    )
    run_id2 = db.start_run()
    db.record_rejection(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Not a leading man type",
        run_id=run_id2,
        platform="aa",
    )
    cursor = db.conn.execute("SELECT COUNT(*) FROM rejected_roles")
    assert cursor.fetchone()[0] == 1
    cursor = db.conn.execute("SELECT rejection_reason FROM rejected_roles")
    assert cursor.fetchone()[0] == "Not a leading man type"


def test_record_application_with_project_url(db):
    db.record_application(
        "role_url", "URL Project", "Lead",
        project_url="https://actorsaccess.com/projects/?breakdown=456",
    )
    cursor = db.conn.execute(
        "SELECT project_url FROM applied_roles WHERE role_id = ?", ("role_url",)
    )
    assert cursor.fetchone()[0] == "https://actorsaccess.com/projects/?breakdown=456"


def test_record_application_project_url_defaults_empty(db):
    db.record_application("role_nourl", "No URL Project", "Lead")
    cursor = db.conn.execute(
        "SELECT project_url FROM applied_roles WHERE role_id = ?", ("role_nourl",)
    )
    assert cursor.fetchone()[0] == ""


def test_get_daily_applications(db):
    """get_daily_applications should return today's applications."""
    db.record_application(
        "role_daily", "Daily Project", "Lead",
        ai_reason="Best fit", project_url="https://example.com",
    )
    rows = db.get_daily_applications()
    assert len(rows) == 1
    assert rows[0]["project_name"] == "Daily Project"


def test_get_daily_rejections(db):
    """get_daily_rejections should return today's rejections."""
    run_id = db.start_run()
    db.record_rejection(
        project_name="Daily Project",
        project_url="https://example.com",
        role_name="Side Character",
        role_description="A friend",
        rejection_reason="Not leading man",
        run_id=run_id,
        platform="aa",
    )
    rows = db.get_daily_rejections()
    assert len(rows) == 1
    assert rows[0]["role_name"] == "Side Character"


def test_get_daily_run_summary(db):
    """get_daily_run_summary should return today's run stats."""
    run_id = db.start_run()
    db.complete_run(run_id, roles_found=10, roles_applied=3, roles_skipped=7)
    summary = db.get_daily_run_summary()
    assert len(summary) >= 1
    assert summary[0]["roles_applied"] == 3


def test_flagged_roles_table_created(db):
    """DB should create flagged_roles table on init."""
    cursor = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert "flagged_roles" in tables


def test_record_flagged_role(db):
    run_id = db.start_run()
    db.record_flagged_role(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Lead",
        role_description="A leading role",
        flag_reason="Needs SAG-AFTRA number",
        run_id=run_id,
        platform="aa",
    )
    cursor = db.conn.execute(
        "SELECT project_name, role_name, flag_reason, platform FROM flagged_roles"
    )
    row = cursor.fetchone()
    assert row[0] == "Test Project"
    assert row[1] == "Lead"
    assert row[2] == "Needs SAG-AFTRA number"
    assert row[3] == "aa"


def test_record_flagged_role_upserts(db):
    """Second flag for same role/project/platform should update reason."""
    run_id = db.start_run()
    db.record_flagged_role(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Lead",
        role_description="A leading role",
        flag_reason="Needs SAG number",
        run_id=run_id,
        platform="aa",
    )
    run_id2 = db.start_run()
    db.record_flagged_role(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Lead",
        role_description="A leading role",
        flag_reason="Needs specific availability dates",
        run_id=run_id2,
        platform="aa",
    )
    cursor = db.conn.execute("SELECT COUNT(*) FROM flagged_roles")
    assert cursor.fetchone()[0] == 1
    cursor = db.conn.execute("SELECT flag_reason FROM flagged_roles")
    assert cursor.fetchone()[0] == "Needs specific availability dates"


def test_get_daily_flagged(db):
    """get_daily_flagged should return today's flagged roles."""
    run_id = db.start_run()
    db.record_flagged_role(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Lead",
        role_description="A leading role",
        flag_reason="Needs SAG number",
        run_id=run_id,
        platform="cn",
    )
    rows = db.get_daily_flagged()
    assert len(rows) == 1
    assert rows[0]["role_name"] == "Lead"
    assert rows[0]["platform"] == "cn"


def test_record_application_default_status_is_submitted(db):
    """No status arg = stored as 'submitted' (backward compatible)."""
    db.record_application("role_def", "Project", "Lead")
    cursor = db.conn.execute(
        "SELECT status FROM applied_roles WHERE role_id = ?", ("role_def",)
    )
    assert cursor.fetchone()[0] == "submitted"


def test_record_application_with_status_draft(db):
    """status='draft' marks the row as an unfinished draft."""
    db.record_application(
        "role_draft", "Project", "Lead",
        platform="backstage", status="draft", submission_note="Paste this cover letter.",
    )
    cursor = db.conn.execute(
        "SELECT status, submission_note FROM applied_roles WHERE role_id = ?",
        ("role_draft",),
    )
    row = cursor.fetchone()
    assert row[0] == "draft"
    assert row[1] == "Paste this cover letter."


def test_is_applied_returns_true_for_draft(db):
    """Drafts must count as applied for dedup so we don't re-create them."""
    db.record_application("role_draft2", "P", "L", platform="backstage", status="draft")
    assert db.is_applied("role_draft2") is True


def test_get_daily_applications_excludes_drafts(db):
    """Drafts should NOT appear in the applied list — they belong to the flagged section."""
    db.record_application("role_sub", "P", "L", platform="backstage")
    db.record_application("role_draft3", "P", "L2", platform="backstage", status="draft")
    rows = db.get_daily_applications()
    role_names = {r["role_name"] for r in rows}
    assert "L" in role_names
    assert "L2" not in role_names


def test_record_flagged_role_with_suggested_note_and_draft_app_id(db):
    run_id = db.start_run()
    db.record_flagged_role(
        project_name="Test Project",
        project_url="https://backstage.com/casting/123/",
        role_name="Fitness Model",
        role_description="Running shoes promo",
        flag_reason="Cover letter required — draft ready in Backstage",
        run_id=run_id,
        platform="backstage",
        suggested_note="I'd be a natural fit for the lifestyle running shoot.",
        draft_app_id=555222,
    )
    rows = db.get_daily_flagged()
    assert len(rows) == 1
    assert rows[0]["suggested_note"].startswith("I'd be a natural fit")
    assert rows[0]["draft_app_id"] == 555222


def test_record_flagged_role_upsert_preserves_draft_app_id(db):
    """Re-flagging the same role (e.g., on retry) should keep the existing draft_app_id
    if the new call doesn't supply one."""
    run_id = db.start_run()
    db.record_flagged_role(
        project_name="Test", project_url="u", role_name="R", role_description="d",
        flag_reason="Cover letter required — draft ready",
        run_id=run_id, platform="backstage",
        suggested_note="note v1", draft_app_id=999,
    )
    db.record_flagged_role(
        project_name="Test", project_url="u", role_name="R", role_description="d",
        flag_reason="Cover letter required — draft preparation failed",
        run_id=run_id, platform="backstage",
        suggested_note="note v2",  # draft_app_id omitted
    )
    rows = db.get_daily_flagged()
    assert len(rows) == 1
    # flag_reason + suggested_note updated but draft_app_id preserved
    assert "draft preparation failed" in rows[0]["flag_reason"]
    assert rows[0]["suggested_note"] == "note v2"
    assert rows[0]["draft_app_id"] == 999


def test_get_all_submission_records_unifies_three_tables(db):
    """One row in each table should produce three unified rows tagged by record_type."""
    db.record_application(
        "role_app", "Applied Project", "Applied Role",
        role_description="desc applied", ai_reason="best fit",
        platform="aa", project_url="https://aa.example/applied",
    )
    db.record_application(
        "role_drft", "Draft Project", "Draft Role",
        role_description="desc draft", platform="backstage", status="draft",
        submission_note="paste this",
    )
    run_id = db.start_run()
    db.record_rejection(
        project_name="Rejected Project", project_url="https://aa.example/rej",
        role_name="Rejected Role", role_description="desc rej",
        rejection_reason="age", run_id=run_id, platform="aa",
    )
    db.record_flagged_role(
        project_name="Flagged Project", project_url="https://aa.example/flag",
        role_name="Flagged Role", role_description="desc flag",
        flag_reason="needs reel", run_id=run_id, platform="cn",
        suggested_note="suggested cover letter",
    )

    rows = db.get_all_submission_records()
    by_type = {r["record_type"]: r for r in rows}

    assert set(by_type) == {"applied", "draft", "flagged", "rejected"}
    assert by_type["applied"]["project_name"] == "Applied Project"
    assert by_type["applied"]["reason"] == "best fit"
    assert by_type["draft"]["submission_note"] == "paste this"
    assert by_type["draft"]["platform"] == "backstage"
    assert by_type["flagged"]["reason"] == "needs reel"
    assert by_type["flagged"]["submission_note"] == "suggested cover letter"
    assert by_type["rejected"]["reason"] == "age"


def test_get_all_submission_records_orders_by_date_desc(db):
    """Rows must come back newest-first regardless of which table they live in."""
    import time
    db.record_application("role_a", "P1", "R1")
    time.sleep(0.01)
    run_id = db.start_run()
    db.record_rejection(
        project_name="P2", project_url="", role_name="R2",
        role_description="", rejection_reason="x", run_id=run_id, platform="aa",
    )
    time.sleep(0.01)
    db.record_application("role_b", "P3", "R3")

    rows = db.get_all_submission_records()
    role_names = [r["role_name"] for r in rows]
    assert role_names == ["R3", "R2", "R1"]


def test_get_all_submission_records_empty(db):
    assert db.get_all_submission_records() == []


def test_schema_migration_adds_new_columns(db):
    """New columns must exist on a freshly created DB."""
    cursor = db.conn.execute("PRAGMA table_info(applied_roles)")
    applied_cols = {row[1] for row in cursor.fetchall()}
    assert "status" in applied_cols

    cursor = db.conn.execute("PRAGMA table_info(flagged_roles)")
    flagged_cols = {row[1] for row in cursor.fetchall()}
    assert "suggested_note" in flagged_cols
    assert "draft_app_id" in flagged_cols
