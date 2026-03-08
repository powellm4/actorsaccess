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
                role_description TEXT,
                ai_reason TEXT,
                candidates_considered INTEGER DEFAULT 1,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                platform TEXT DEFAULT 'aa'
            );
            CREATE TABLE IF NOT EXISTS run_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                roles_found INTEGER DEFAULT 0,
                roles_applied INTEGER DEFAULT 0,
                roles_skipped INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                error_message TEXT,
                platform TEXT DEFAULT 'aa'
            );
        """)
        # Add columns if upgrading from older schema
        try:
            self.conn.execute("ALTER TABLE applied_roles ADD COLUMN role_description TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE applied_roles ADD COLUMN ai_reason TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE applied_roles ADD COLUMN candidates_considered INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE applied_roles ADD COLUMN platform TEXT DEFAULT 'aa'")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE run_history ADD COLUMN platform TEXT DEFAULT 'aa'")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def is_applied(self, role_id: str) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM applied_roles WHERE role_id = ?", (role_id,)
        )
        return cursor.fetchone() is not None

    def record_application(
        self, role_id: str, project_name: str, role_name: str,
        role_description: str = "", ai_reason: str = "", candidates_considered: int = 1,
        platform: str = "aa",
    ):
        self.conn.execute(
            """INSERT OR IGNORE INTO applied_roles
               (role_id, project_name, role_name, role_description, ai_reason, candidates_considered, platform)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (role_id, project_name, role_name, role_description, ai_reason, candidates_considered, platform),
        )
        self.conn.commit()

    def start_run(self, platform: str = "aa") -> int:
        cursor = self.conn.execute(
            "INSERT INTO run_history (started_at, platform) VALUES (?, ?)",
            (datetime.now().isoformat(), platform),
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
