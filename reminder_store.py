"""reminder_store.py — SQLite-backed reminder row CRUD for FR-7.

The reminder engine (`reminder_engine.py`) is the primary consumer:
  - `bulk_create_for_task()` inserts N rows (one per offset) when a task is created.
  - `list_ready_to_fire()` fetches rows due for delivery (called every minute).
  - `mark_fired()` / `mark_missed()` / `cancel_for_task()` transition row status.

Snoozed reminders are inserted as new rows with `kind='snoozed'` and
`offset_seconds=0` so they can be queried / cancelled independently.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from db.connection import get_connection


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class SqliteReminderStore:
    """SQLite adapter for the task_reminders table."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Create ────────────────────────────────────────────────────────────────

    def bulk_create_for_task(
        self,
        task_id: int,
        deadline_iso: str,
        offset_seconds_list: list[int],
    ) -> list[dict]:
        """Insert one reminder row per offset. Returns list of created dicts.

        fire_at for each row = deadline - offset_seconds.
        Rows with fire_at in the past are still inserted; the reminder engine
        applies the 1-hour grace window (D12) to decide whether to fire or skip.

        Args:
            task_id: the parent task's id.
            deadline_iso: ISO datetime string (VN TZ) of the task deadline.
            offset_seconds_list: list of seconds before deadline to fire;
                                  e.g. [7200, 3600, 1800, 900].
        """
        from datetime import timedelta

        deadline_dt = _parse_iso(deadline_iso)
        now = _utcnow_iso()
        rows_data = []
        for offset in offset_seconds_list:
            fire_at_dt = deadline_dt - timedelta(seconds=offset)
            fire_at_iso = fire_at_dt.strftime("%Y-%m-%d %H:%M:%S")
            rows_data.append((task_id, fire_at_iso, offset, "scheduled", "pending", now))

        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO task_reminders (task_id, fire_at, offset_seconds, kind, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows_data,
            )

        # Fetch the newly inserted rows (last N rows for this task_id, ordered by fire_at).
        rows = self._conn.execute(
            """
            SELECT * FROM task_reminders
            WHERE task_id = ? AND kind = 'scheduled'
            ORDER BY fire_at ASC
            """,
            (task_id,),
        ).fetchall()
        # Return only the most recently created batch (matching count).
        return [_row_to_dict(r) for r in rows[-len(offset_seconds_list):]]

    def create_snoozed(
        self,
        task_id: int,
        fire_at_iso: str,
    ) -> dict:
        """Insert a snoozed reminder row. Returns the created dict.

        Snoozed reminders have kind='snoozed' and offset_seconds=0.
        The caller is responsible for checking max snooze count (D6).
        """
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO task_reminders (task_id, fire_at, offset_seconds, kind, status, created_at)
                VALUES (?, ?, 0, 'snoozed', 'pending', ?)
                """,
                (task_id, fire_at_iso, now),
            )
        return self.get_reminder(cur.lastrowid)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_reminder(self, reminder_id: int) -> dict | None:
        """Return full reminder dict, or None if not found."""
        row = self._conn.execute(
            "SELECT * FROM task_reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_for_task(self, task_id: int) -> list[dict]:
        """Return all reminder rows for a task ordered by fire_at ASC."""
        rows = self._conn.execute(
            "SELECT * FROM task_reminders WHERE task_id = ? ORDER BY fire_at ASC",
            (task_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_ready_to_fire(
        self,
        now_iso: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Return pending reminders with fire_at <= now_iso, oldest first.

        Used by the reminder engine tick (called every minute).
        Includes both 'scheduled' and 'snoozed' kinds.
        """
        if limit <= 0:
            return []
        now = now_iso or _utcnow_iso()
        rows = self._conn.execute(
            """
            SELECT tr.*, t.user_id, t.title, t.deadline,
                   t.recurring_rule, t.status AS task_status,
                   t.snooze_count
            FROM task_reminders tr
            JOIN tasks t ON t.id = tr.task_id
            WHERE tr.status = 'pending'
              AND tr.fire_at <= ?
              AND t.deleted_at IS NULL
            ORDER BY tr.fire_at ASC, tr.id ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def count_pending_for_task(self, task_id: int) -> int:
        """Return count of pending reminder rows for a task."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM task_reminders WHERE task_id = ? AND status = 'pending'",
            (task_id,),
        ).fetchone()
        return row[0] if row else 0

    # ── Status transitions ────────────────────────────────────────────────────

    def mark_fired(self, reminder_id: int, fired_at: str | None = None) -> bool:
        """Transition reminder to status='fired'. Returns True if updated."""
        now = fired_at or _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE task_reminders
                SET status = 'fired', fired_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, reminder_id),
            )
        return cur.rowcount > 0

    def mark_missed(self, reminder_id: int) -> bool:
        """Transition reminder to status='missed' (grace window expired, D12).

        Returns True if updated.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE task_reminders SET status = 'missed' WHERE id = ? AND status = 'pending'",
                (reminder_id,),
            )
        return cur.rowcount > 0

    def cancel_for_task(self, task_id: int) -> int:
        """Cancel all pending reminders for a task. Returns count of rows updated.

        Called when a task is completed, cancelled, or soft-deleted.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE task_reminders SET status = 'cancelled' WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            )
        return cur.rowcount


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_iso(iso: str) -> datetime:
    """Parse an ISO datetime string; attach UTC if naive."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
