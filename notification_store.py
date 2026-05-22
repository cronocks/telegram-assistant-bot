"""notification_store.py — SQLite-backed pending notification queue (FR-4 sub 4.5).

Producers call `enqueue(...)` to insert a notification row in `status='pending'`.
The scheduled flush job (in `notification_service`) calls `get_pending_ready()`
to fetch rows due for delivery, then transitions them via `mark_delivered` /
`record_failed_attempt`.

Exponential backoff lives here: on each failure, `next_retry_at` is set to
`now + 2^attempts minutes`. After `max_attempts` (default 5) the row's status
transitions to `'failed'` permanently — no further retries.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from db.connection import get_connection


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class SqliteNotificationStore:
    """SQLite adapter for the pending_notifications queue."""

    # Truncation cap for last_error to keep audit/log readable.
    _ERROR_CAP = 500

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Write ─────────────────────────────────────────────────────────────────

    def enqueue(
        self,
        user_id: int,
        channel: str,
        payload: dict[str, Any],
    ) -> int:
        """Insert a pending notification. Returns the new row id."""
        if not channel:
            raise ValueError("notification.enqueue: channel must be non-empty")
        payload_text = json.dumps(payload, ensure_ascii=False)
        cur = self._conn.execute(
            "INSERT INTO pending_notifications (user_id, channel, payload)"
            " VALUES (?, ?, ?)",
            (user_id, channel, payload_text),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def mark_delivered(
        self, notif_id: int, now: datetime | None = None,
    ) -> bool:
        """Transition row to status='delivered'. Idempotent on already-delivered.

        Returns True if the row was updated this call.
        """
        now = now or _utcnow()
        cur = self._conn.execute(
            "UPDATE pending_notifications"
            " SET status = 'delivered', delivered_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (_iso(now), notif_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def record_failed_attempt(
        self,
        notif_id: int,
        error: str,
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> dict:
        """Increment attempts; either schedule retry or mark final-failed.

        Returns:
            {
              "attempts":      int (new value after increment),
              "final":         bool (True if status was set to 'failed'),
              "next_retry_at": ISO string or None,
            }
        """
        now = now or _utcnow()
        row = self.get_by_id(notif_id)
        if row is None:
            raise ValueError(f"notification {notif_id} not found")

        new_attempts = row["attempts"] + 1
        truncated_error = (error or "")[: self._ERROR_CAP]

        if new_attempts >= max_attempts:
            self._conn.execute(
                "UPDATE pending_notifications"
                " SET status = 'failed', attempts = ?, last_error = ?,"
                "     next_retry_at = NULL"
                " WHERE id = ?",
                (new_attempts, truncated_error, notif_id),
            )
            self._conn.commit()
            return {"attempts": new_attempts, "final": True, "next_retry_at": None}

        # Schedule retry: now + 2^attempts minutes (2, 4, 8, 16, ...).
        next_retry = now + timedelta(minutes=2 ** new_attempts)
        next_retry_iso = _iso(next_retry)
        self._conn.execute(
            "UPDATE pending_notifications"
            " SET attempts = ?, last_error = ?, next_retry_at = ?"
            " WHERE id = ?",
            (new_attempts, truncated_error, next_retry_iso, notif_id),
        )
        self._conn.commit()
        return {
            "attempts": new_attempts,
            "final": False,
            "next_retry_at": next_retry_iso,
        }

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_by_id(self, notif_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT id, user_id, channel, payload, status, attempts, last_error,"
            "       next_retry_at, created_at, delivered_at"
            " FROM pending_notifications WHERE id = ?",
            (notif_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def get_pending_ready(
        self,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return pending notifications ready for delivery.

        Ready = status='pending' AND (next_retry_at IS NULL OR next_retry_at <= now).
        Ordered by created_at ASC (oldest first — FIFO under contention).
        """
        if limit <= 0:
            return []
        now = now or _utcnow()
        rows = self._conn.execute(
            "SELECT id, user_id, channel, payload, status, attempts, last_error,"
            "       next_retry_at, created_at, delivered_at"
            " FROM pending_notifications"
            " WHERE status = 'pending'"
            "   AND (next_retry_at IS NULL OR next_retry_at <= ?)"
            " ORDER BY created_at ASC, id ASC"
            " LIMIT ?",
            (_iso(now), limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row) -> dict:
        keys = [
            "id", "user_id", "channel", "payload", "status", "attempts",
            "last_error", "next_retry_at", "created_at", "delivered_at",
        ]
        return dict(zip(keys, row))
