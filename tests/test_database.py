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
