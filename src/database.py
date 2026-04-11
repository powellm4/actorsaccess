# src/database.py
import sqlite3
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)


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
            CREATE TABLE IF NOT EXISTS rejected_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT,
                project_url TEXT DEFAULT '',
                role_name TEXT,
                role_description TEXT,
                rejection_reason TEXT,
                run_id INTEGER,
                platform TEXT DEFAULT 'aa',
                rejected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(role_name, project_name, platform)
            );
            CREATE TABLE IF NOT EXISTS flagged_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT,
                project_url TEXT DEFAULT '',
                role_name TEXT,
                role_description TEXT,
                flag_reason TEXT,
                run_id INTEGER,
                platform TEXT DEFAULT 'aa',
                flagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(role_name, project_name, platform)
            );
            CREATE TABLE IF NOT EXISTS digest_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        try:
            self.conn.execute("ALTER TABLE applied_roles ADD COLUMN project_url TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE applied_roles ADD COLUMN submission_note TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # Mode column: 'paid' (default) or 'unpaid'. Tracks which workflow
        # applied or rejected the role. Does NOT affect dedup — role_id UNIQUE
        # still prevents double-applying across modes.
        try:
            self.conn.execute("ALTER TABLE applied_roles ADD COLUMN mode TEXT DEFAULT 'paid'")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE run_history ADD COLUMN mode TEXT DEFAULT 'paid'")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE rejected_roles ADD COLUMN mode TEXT DEFAULT 'paid'")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE flagged_roles ADD COLUMN mode TEXT DEFAULT 'paid'")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def has_seen_breakdown(self, breakdown_id: str, platform: str = "aa") -> bool:
        """Check if we've processed any role from this breakdown in a previous run."""
        # Check applied_roles (role_id format: {breakdown_id}_{role_id})
        cursor = self.conn.execute(
            "SELECT 1 FROM applied_roles WHERE role_id LIKE ? AND platform = ?",
            (f"{breakdown_id}_%", platform),
        )
        if cursor.fetchone():
            return True
        # Check rejected_roles and flagged_roles by project_name isn't reliable
        # since breakdown_id isn't stored there — but applied + already_submitted
        # covers the common cases. Also check rejected/flagged via role_id pattern
        # stored in applied_roles is our best signal.
        return False

    def is_applied(self, role_id: str) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM applied_roles WHERE role_id = ?", (role_id,)
        )
        return cursor.fetchone() is not None

    def is_rejected(self, role_name: str, project_name: str, platform: str = "aa") -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM rejected_roles WHERE role_name = ? AND project_name = ? AND platform = ?",
            (role_name, project_name, platform),
        )
        return cursor.fetchone() is not None

    def record_application(
        self, role_id: str, project_name: str, role_name: str,
        role_description: str = "", ai_reason: str = "", candidates_considered: int = 1,
        platform: str = "aa", project_url: str = "", submission_note: str = "",
        mode: str = "paid",
    ):
        logger.info(f"[DB] Recording application: {project_name} — {role_name} (id={role_id}, mode={mode})")
        self.conn.execute(
            """INSERT OR IGNORE INTO applied_roles
               (role_id, project_name, role_name, role_description, ai_reason, candidates_considered, platform, project_url, applied_at, submission_note, mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (role_id, project_name, role_name, role_description, ai_reason, candidates_considered, platform, project_url, self._utcnow(), submission_note, mode),
        )
        self.conn.commit()

    def record_rejection(
        self, project_name: str, project_url: str, role_name: str,
        role_description: str, rejection_reason: str, run_id: int, platform: str = "aa",
        mode: str = "paid",
    ):
        logger.info(f"[DB] Recording rejection: {project_name} — {role_name} ({rejection_reason}, mode={mode})")
        now = self._utcnow()
        self.conn.execute(
            """INSERT INTO rejected_roles
               (project_name, project_url, role_name, role_description, rejection_reason, run_id, platform, rejected_at, mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(role_name, project_name, platform) DO UPDATE SET
                   rejection_reason = excluded.rejection_reason,
                   role_description = excluded.role_description,
                   run_id = excluded.run_id,
                   project_url = excluded.project_url,
                   mode = excluded.mode""",
            (project_name, project_url, role_name, role_description, rejection_reason, run_id, platform, now, mode),
        )
        self.conn.commit()

    def _utcnow(self) -> str:
        """UTC timestamp with microsecond precision for reliable ordering."""
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    def start_run(self, platform: str = "aa", mode: str = "paid") -> int:
        cursor = self.conn.execute(
            "INSERT INTO run_history (started_at, platform, mode) VALUES (?, ?, ?)",
            (self._utcnow(), platform, mode),
        )
        self.conn.commit()
        logger.info(f"[DB] Started run id={cursor.lastrowid} platform={platform} mode={mode}")
        return cursor.lastrowid

    def complete_run(self, run_id: int, roles_found: int, roles_applied: int, roles_skipped: int):
        logger.info(f"[DB] Completed run id={run_id}: found={roles_found}, applied={roles_applied}, skipped={roles_skipped}")
        self.conn.execute(
            """UPDATE run_history
               SET completed_at = ?, roles_found = ?, roles_applied = ?,
                   roles_skipped = ?, status = 'success'
               WHERE id = ?""",
            (self._utcnow(), roles_found, roles_applied, roles_skipped, run_id),
        )
        self.conn.commit()

    def fail_run(self, run_id: int, error_message: str):
        logger.info(f"[DB] Failed run id={run_id}: {error_message}")
        self.conn.execute(
            """UPDATE run_history
               SET completed_at = ?, status = 'error', error_message = ?
               WHERE id = ?""",
            (self._utcnow(), error_message, run_id),
        )
        self.conn.commit()

    def get_last_digest_time(self) -> str:
        """Return the timestamp of the last digest sent, or 24 hours ago if none."""
        cursor = self.conn.execute(
            "SELECT sent_at FROM digest_history ORDER BY sent_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def record_digest_sent(self):
        """Record that a digest was just sent."""
        self.conn.execute(
            "INSERT INTO digest_history (sent_at) VALUES (?)",
            (self._utcnow(),),
        )
        self.conn.commit()

    def _since_clause(self) -> str:
        """Return a timestamp string for 'since last digest' or fallback to 24h."""
        last = self.get_last_digest_time()
        if last:
            return last
        return "datetime('now', '-24 hours')"

    def get_daily_applications(self, mode: str | None = None) -> list[dict]:
        since = self.get_last_digest_time()
        mode_clause = " AND mode = ?" if mode else ""
        mode_params = (mode,) if mode else ()
        if since:
            query = f"""SELECT project_name, role_name, role_description, ai_reason,
                              candidates_considered, platform, project_url, applied_at, submission_note, mode
                       FROM applied_roles
                       WHERE applied_at > ?{mode_clause}
                       ORDER BY applied_at DESC"""
            cursor = self.conn.execute(query, (since,) + mode_params)
        else:
            query = f"""SELECT project_name, role_name, role_description, ai_reason,
                          candidates_considered, platform, project_url, applied_at, submission_note, mode
                   FROM applied_roles
                   WHERE applied_at >= datetime('now', '-24 hours'){mode_clause}
                   ORDER BY applied_at DESC"""
            cursor = self.conn.execute(query, mode_params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_daily_rejections(self, mode: str | None = None) -> list[dict]:
        since = self.get_last_digest_time()
        mode_clause = " AND mode = ?" if mode else ""
        mode_params = (mode,) if mode else ()
        if since:
            query = f"""SELECT project_name, role_name, role_description, rejection_reason,
                              platform, project_url, rejected_at, mode
                       FROM rejected_roles
                       WHERE rejected_at > ?{mode_clause}
                       ORDER BY rejected_at DESC"""
            cursor = self.conn.execute(query, (since,) + mode_params)
        else:
            query = f"""SELECT project_name, role_name, role_description, rejection_reason,
                          platform, project_url, rejected_at, mode
                   FROM rejected_roles
                   WHERE rejected_at >= datetime('now', '-24 hours'){mode_clause}
                   ORDER BY rejected_at DESC"""
            cursor = self.conn.execute(query, mode_params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_daily_run_summary(self, mode: str | None = None) -> list[dict]:
        since = self.get_last_digest_time()
        mode_clause = " AND mode = ?" if mode else ""
        mode_params = (mode,) if mode else ()
        if since:
            query = f"""SELECT platform, status, roles_found, roles_applied, roles_skipped,
                              error_message, started_at, completed_at, mode
                       FROM run_history
                       WHERE started_at > ?{mode_clause}
                       ORDER BY started_at DESC"""
            cursor = self.conn.execute(query, (since,) + mode_params)
        else:
            query = f"""SELECT platform, status, roles_found, roles_applied, roles_skipped,
                          error_message, started_at, completed_at, mode
                   FROM run_history
                   WHERE started_at >= datetime('now', '-24 hours'){mode_clause}
                   ORDER BY started_at DESC"""
            cursor = self.conn.execute(query, mode_params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def record_flagged_role(
        self, project_name: str, project_url: str, role_name: str,
        role_description: str, flag_reason: str, run_id: int, platform: str = "aa",
        mode: str = "paid",
    ):
        logger.info(f"[DB] Recording flagged role: {project_name} — {role_name} ({flag_reason}, mode={mode})")
        now = self._utcnow()
        self.conn.execute(
            """INSERT INTO flagged_roles
               (project_name, project_url, role_name, role_description, flag_reason, run_id, platform, flagged_at, mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(role_name, project_name, platform) DO UPDATE SET
                   flag_reason = excluded.flag_reason,
                   role_description = excluded.role_description,
                   run_id = excluded.run_id,
                   project_url = excluded.project_url,
                   flagged_at = flagged_roles.flagged_at,
                   mode = excluded.mode""",
            (project_name, project_url, role_name, role_description, flag_reason, run_id, platform, now, mode),
        )
        self.conn.commit()

    def get_daily_flagged(self, mode: str | None = None) -> list[dict]:
        since = self.get_last_digest_time()
        mode_clause = " AND mode = ?" if mode else ""
        mode_params = (mode,) if mode else ()
        if since:
            query = f"""SELECT project_name, role_name, role_description, flag_reason,
                              platform, project_url, flagged_at, mode
                       FROM flagged_roles
                       WHERE flagged_at > ?{mode_clause}
                       ORDER BY flagged_at DESC"""
            cursor = self.conn.execute(query, (since,) + mode_params)
        else:
            query = f"""SELECT project_name, role_name, role_description, flag_reason,
                          platform, project_url, flagged_at, mode
                   FROM flagged_roles
                   WHERE flagged_at >= datetime('now', '-24 hours'){mode_clause}
                   ORDER BY flagged_at DESC"""
            cursor = self.conn.execute(query, mode_params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def close(self):
        self.conn.close()
