"""task_store.py — SQLite-backed task CRUD for FR-7.

Producers (Telegram commands, web routes) call `create_task()` to insert a row
and receive the new task dict. The reminder engine and daily summary jobs call
`list_pending_due()` / `list_for_user()` to read tasks. Write paths use
`with self._conn:` context managers for atomic commits.

All datetimes are stored as ISO strings (VN timezone, Asia/Ho_Chi_Minh).
Callers are responsible for timezone conversion before passing deadlines in.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from db.connection import get_connection


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class SqliteTaskStore:
    """SQLite adapter for the tasks table."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Create ────────────────────────────────────────────────────────────────

    def create_task(
        self,
        user_id: int,
        title: str,
        deadline: str,
        *,
        description: str | None = None,
        category: str = "task",
        scope: str = "private",
        recurring_rule: str | None = None,
        reminder_offsets: str = "7200,3600,1800,900",
        source: str = "telegram",
    ) -> dict:
        """Insert a new task row. Returns the full task dict.

        Raises ValueError if title or deadline are empty.
        """
        if not title or not title.strip():
            raise ValueError("task.create_task: title must be non-empty")
        if not deadline or not deadline.strip():
            raise ValueError("task.create_task: deadline must be non-empty")

        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO tasks (
                    user_id, title, description, deadline,
                    category, scope, recurring_rule, reminder_offsets,
                    status, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    user_id, title.strip(), description, deadline,
                    category, scope, recurring_rule, reminder_offsets,
                    source, now, now,
                ),
            )
        return self.get_task(cur.lastrowid)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_task(self, task_id: int) -> dict | None:
        """Return full task dict, or None if not found (including soft-deleted)."""
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_for_user(
        self,
        user_id: int,
        *,
        status: str | None = None,
        category: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict]:
        """Return tasks for user_id ordered by deadline ASC.

        Args:
            status: filter by status ('pending'|'completed'|'cancelled'); None = all.
            category: filter by category ('task'|'study'|'reminder'); None = all.
            include_deleted: include soft-deleted rows when True.
        """
        conditions = ["user_id = ?"]
        params: list = [user_id]

        if not include_deleted:
            conditions.append("deleted_at IS NULL")
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if category is not None:
            conditions.append("category = ?")
            params.append(category)

        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE {where} ORDER BY deadline ASC",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_pending_due(
        self,
        before_iso: str,
        *,
        user_id: int | None = None,
    ) -> list[dict]:
        """Return active pending tasks with deadline <= before_iso.

        Used by daily summary and reminder engine to find tasks due soon.
        Excludes soft-deleted tasks.
        """
        conditions = [
            "status = 'pending'",
            "deleted_at IS NULL",
            "deadline <= ?",
        ]
        params: list = [before_iso]

        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)

        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE {where} ORDER BY deadline ASC",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_completed_on(self, user_id: int, date_prefix: str) -> list[dict]:
        """Return tasks completed on a given date (date_prefix format: 'YYYY-MM-DD').

        Used by daily summary to report what was done today.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM tasks
            WHERE user_id = ?
              AND status = 'completed'
              AND completed_at LIKE ?
              AND deleted_at IS NULL
            ORDER BY completed_at ASC
            """,
            (user_id, f"{date_prefix}%"),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Update ────────────────────────────────────────────────────────────────

    def update_task(self, task_id: int, **fields) -> dict | None:
        """Update allowed task fields. Returns updated dict, or None if not found.

        Allowed fields: title, description, deadline, category, scope,
                        recurring_rule, reminder_offsets, status.
        Always bumps updated_at.
        """
        allowed = {
            "title", "description", "deadline", "category", "scope",
            "recurring_rule", "reminder_offsets", "status",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_task(task_id)

        updates["updated_at"] = _utcnow_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]

        with self._conn:
            self._conn.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?", values
            )
        return self.get_task(task_id)

    def complete_task(self, task_id: int, completed_at: str | None = None) -> dict | None:
        """Mark a task as completed. Returns updated dict, or None if not found.

        Also cancels all pending reminders for this task.
        """
        now = completed_at or _utcnow_iso()
        with self._conn:
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, _utcnow_iso(), task_id),
            )
            # Cancel all pending reminders for this task.
            self._conn.execute(
                "UPDATE task_reminders SET status = 'cancelled' WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            )
        return self.get_task(task_id)

    def cancel_task(self, task_id: int) -> dict | None:
        """Mark a task as cancelled. Returns updated dict, or None if not found.

        Also cancels all pending reminders for this task.
        """
        now = _utcnow_iso()
        with self._conn:
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, task_id),
            )
            self._conn.execute(
                "UPDATE task_reminders SET status = 'cancelled' WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            )
        return self.get_task(task_id)

    def increment_snooze(self, task_id: int) -> int:
        """Increment snooze_count for a task. Returns new snooze_count value.

        Caller checks the returned value against max (3) before allowing snooze.
        """
        now = _utcnow_iso()
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET snooze_count = snooze_count + 1, updated_at = ? WHERE id = ?",
                (now, task_id),
            )
        row = self._conn.execute(
            "SELECT snooze_count FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return row["snooze_count"] if row else 0

    # ── Soft-delete ───────────────────────────────────────────────────────────

    def soft_delete_task(self, task_id: int) -> bool:
        """Soft-delete a task. Returns True if a row was updated.

        Also cancels all pending reminders for this task.
        """
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now, now, task_id),
            )
            if cur.rowcount > 0:
                self._conn.execute(
                    "UPDATE task_reminders SET status = 'cancelled' WHERE task_id = ? AND status = 'pending'",
                    (task_id,),
                )
        return cur.rowcount > 0

    def restore_task(self, task_id: int) -> bool:
        """Restore a soft-deleted task. Returns True if a row was updated."""
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET deleted_at = NULL, updated_at = ? WHERE id = ? AND deleted_at IS NOT NULL",
                (now, task_id),
            )
        return cur.rowcount > 0
