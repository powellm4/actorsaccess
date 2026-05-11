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
            CREATE TABLE IF NOT EXISTS shadow_comparisons (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id                    INTEGER,
                platform                  TEXT NOT NULL,
                mode                      TEXT NOT NULL,
                call_site                 TEXT NOT NULL,
                project_name              TEXT,
                role_name                 TEXT,
                prompt_hash               TEXT NOT NULL,
                prompt_text               TEXT NOT NULL,

                claude_response           TEXT,
                claude_verdict            TEXT,
                claude_latency_ms         INTEGER,
                claude_input_tokens       INTEGER,
                claude_output_tokens      INTEGER,

                ds_chat_response          TEXT,
                ds_chat_verdict           TEXT,
                ds_chat_latency_ms        INTEGER,
                ds_chat_input_tokens      INTEGER,
                ds_chat_output_tokens     INTEGER,
                ds_chat_error             TEXT,

                ds_reasoner_response      TEXT,
                ds_reasoner_verdict       TEXT,
                ds_reasoner_latency_ms    INTEGER,
                ds_reasoner_input_tokens  INTEGER,
                ds_reasoner_output_tokens INTEGER,
                ds_reasoner_error         TEXT,

                chat_matches_claude       INTEGER,
                reasoner_matches_claude   INTEGER,

                user_adjudication         TEXT,
                user_adjudication_note    TEXT,

                created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_call_site     ON shadow_comparisons(call_site);
            CREATE INDEX IF NOT EXISTS idx_shadow_created_at    ON shadow_comparisons(created_at);
            CREATE INDEX IF NOT EXISTS idx_shadow_disagreements ON shadow_comparisons(chat_matches_claude, reasoner_matches_claude);
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
        # status='submitted' (default, existing behavior) or 'draft' (prepare-only,
        # e.g. Backstage cover-letter-required roles that the user must finalize
        # manually). is_applied() ignores the column so dedup still works.
        try:
            self.conn.execute("ALTER TABLE applied_roles ADD COLUMN status TEXT DEFAULT 'submitted'")
        except sqlite3.OperationalError:
            pass
        # suggested_note: AI-drafted cover letter text shown in the digest so
        # the user can copy/paste/edit when finalizing the draft on Backstage.
        try:
            self.conn.execute("ALTER TABLE flagged_roles ADD COLUMN suggested_note TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # draft_app_id: Backstage application id of the prepared-only draft,
        # used by the digest to render an "Open on Backstage" link.
        try:
            self.conn.execute("ALTER TABLE flagged_roles ADD COLUMN draft_app_id INTEGER")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def has_seen_breakdown(self, breakdown_id: str, platform: str = "aa", mode: str | None = None) -> bool:
        """Check if we've processed any role from this breakdown in a previous run.

        If mode is provided, only count rows with a matching mode column.
        Unpaid mode should pass mode='unpaid' so it doesn't inherit paid
        mode's already-processed pool — otherwise unpaid mode's early-exit
        "consecutive seen listings" guard trips immediately on AA.
        """
        # Check applied_roles (role_id format: {breakdown_id}_{role_id})
        if mode is None:
            cursor = self.conn.execute(
                "SELECT 1 FROM applied_roles WHERE role_id LIKE ? AND platform = ?",
                (f"{breakdown_id}_%", platform),
            )
        else:
            cursor = self.conn.execute(
                "SELECT 1 FROM applied_roles WHERE role_id LIKE ? AND platform = ? AND mode = ?",
                (f"{breakdown_id}_%", platform, mode),
            )
        if cursor.fetchone():
            return True
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
        mode: str = "paid", status: str = "submitted",
    ):
        logger.info(f"[DB] Recording application: {project_name} — {role_name} (id={role_id}, mode={mode}, status={status})")
        self.conn.execute(
            """INSERT OR IGNORE INTO applied_roles
               (role_id, project_name, role_name, role_description, ai_reason, candidates_considered, platform, project_url, applied_at, submission_note, mode, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (role_id, project_name, role_name, role_description, ai_reason, candidates_considered, platform, project_url, self._utcnow(), submission_note, mode, status),
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
        """Return applications with status='submitted' from the last window.
        Drafts (status='draft') are excluded — they surface in get_daily_flagged."""
        since = self.get_last_digest_time()
        mode_clause = " AND mode = ?" if mode else ""
        mode_params = (mode,) if mode else ()
        if since:
            query = f"""SELECT project_name, role_name, role_description, ai_reason,
                              candidates_considered, platform, project_url, applied_at, submission_note, mode, status
                       FROM applied_roles
                       WHERE applied_at > ?{mode_clause}
                         AND COALESCE(status, 'submitted') = 'submitted'
                       ORDER BY applied_at DESC"""
            cursor = self.conn.execute(query, (since,) + mode_params)
        else:
            query = f"""SELECT project_name, role_name, role_description, ai_reason,
                          candidates_considered, platform, project_url, applied_at, submission_note, mode, status
                   FROM applied_roles
                   WHERE applied_at >= datetime('now', '-24 hours'){mode_clause}
                     AND COALESCE(status, 'submitted') = 'submitted'
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
        mode: str = "paid", suggested_note: str = "", draft_app_id: int | None = None,
    ):
        logger.info(f"[DB] Recording flagged role: {project_name} — {role_name} ({flag_reason}, mode={mode})")
        now = self._utcnow()
        self.conn.execute(
            """INSERT INTO flagged_roles
               (project_name, project_url, role_name, role_description, flag_reason, run_id, platform, flagged_at, mode, suggested_note, draft_app_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(role_name, project_name, platform) DO UPDATE SET
                   flag_reason = excluded.flag_reason,
                   role_description = excluded.role_description,
                   run_id = excluded.run_id,
                   project_url = excluded.project_url,
                   flagged_at = flagged_roles.flagged_at,
                   mode = excluded.mode,
                   suggested_note = excluded.suggested_note,
                   draft_app_id = COALESCE(excluded.draft_app_id, flagged_roles.draft_app_id)""",
            (project_name, project_url, role_name, role_description, flag_reason, run_id, platform, now, mode, suggested_note, draft_app_id),
        )
        self.conn.commit()

    def get_daily_flagged(self, mode: str | None = None) -> list[dict]:
        since = self.get_last_digest_time()
        mode_clause = " AND mode = ?" if mode else ""
        mode_params = (mode,) if mode else ()
        if since:
            query = f"""SELECT project_name, role_name, role_description, flag_reason,
                              platform, project_url, flagged_at, mode, suggested_note, draft_app_id
                       FROM flagged_roles
                       WHERE flagged_at > ?{mode_clause}
                       ORDER BY flagged_at DESC"""
            cursor = self.conn.execute(query, (since,) + mode_params)
        else:
            query = f"""SELECT project_name, role_name, role_description, flag_reason,
                          platform, project_url, flagged_at, mode, suggested_note, draft_app_id
                   FROM flagged_roles
                   WHERE flagged_at >= datetime('now', '-24 hours'){mode_clause}
                   ORDER BY flagged_at DESC"""
            cursor = self.conn.execute(query, mode_params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_all_submission_records(self) -> list[dict]:
        """Return every record from applied_roles, flagged_roles, and rejected_roles.

        Used by the searchable archive attached to each daily digest. Each row
        carries a `record_type` discriminator ('applied', 'draft', 'flagged',
        'rejected') and a unified `date_iso` column so the archive can render
        a single sortable table. Ordered by date desc (most recent first).
        """
        query = """
            SELECT
                CASE WHEN COALESCE(status, 'submitted') = 'draft'
                     THEN 'draft' ELSE 'applied' END AS record_type,
                applied_at AS date_iso,
                platform, mode, project_name, project_url,
                role_name, role_description,
                ai_reason AS reason,
                submission_note
            FROM applied_roles
            UNION ALL
            SELECT
                'flagged' AS record_type,
                flagged_at AS date_iso,
                platform, mode, project_name, project_url,
                role_name, role_description,
                flag_reason AS reason,
                suggested_note AS submission_note
            FROM flagged_roles
            UNION ALL
            SELECT
                'rejected' AS record_type,
                rejected_at AS date_iso,
                platform, mode, project_name, project_url,
                role_name, role_description,
                rejection_reason AS reason,
                '' AS submission_note
            FROM rejected_roles
            ORDER BY date_iso DESC
        """
        cursor = self.conn.execute(query)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def close(self):
        self.conn.close()
