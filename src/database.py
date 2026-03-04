# src/database.py
import sqlite3
from datetime import datetime


class Database:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS applied_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role_id TEXT UNIQUE,
                project_name TEXT,
                role_name TEXT,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS run_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                roles_found INTEGER DEFAULT 0,
                roles_applied INTEGER DEFAULT 0,
                roles_skipped INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                error_message TEXT
            );
        """)
        self.conn.commit()

    def is_applied(self, role_id: str) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM applied_roles WHERE role_id = ?", (role_id,)
        )
        return cursor.fetchone() is not None

    def record_application(self, role_id: str, project_name: str, role_name: str):
        self.conn.execute(
            """INSERT OR IGNORE INTO applied_roles (role_id, project_name, role_name)
               VALUES (?, ?, ?)""",
            (role_id, project_name, role_name),
        )
        self.conn.commit()

    def start_run(self) -> int:
        cursor = self.conn.execute(
            "INSERT INTO run_history (started_at) VALUES (?)",
            (datetime.now().isoformat(),),
        )
        self.conn.commit()
        return cursor.lastrowid

    def complete_run(self, run_id: int, roles_found: int, roles_applied: int, roles_skipped: int):
        self.conn.execute(
            """UPDATE run_history
               SET completed_at = ?, roles_found = ?, roles_applied = ?,
                   roles_skipped = ?, status = 'success'
               WHERE id = ?""",
            (datetime.now().isoformat(), roles_found, roles_applied, roles_skipped, run_id),
        )
        self.conn.commit()

    def fail_run(self, run_id: int, error_message: str):
        self.conn.execute(
            """UPDATE run_history
               SET completed_at = ?, status = 'error', error_message = ?
               WHERE id = ?""",
            (datetime.now().isoformat(), error_message, run_id),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
