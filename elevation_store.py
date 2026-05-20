"""elevation_store.py — SQLite-backed privilege elevation (sudo) sessions.

Each (channel, chat_id) pair can hold at most one active elevation session.
Sessions expire after `ttl_minutes` (default 15) via lazy filtering — there is
no background cleanup job; `get_active_session` simply ignores rows whose
`expires_at` is in the past.

Failed sudo attempts are tracked separately for rate-limiting: after
`max_fails` wrong passwords, the (channel, chat_id) is locked for
`lockout_minutes`. A successful sudo resets the counter.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import config
from db.connection import get_connection


class SqliteElevationStore:
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Time helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Session lifecycle ────────────────────────────────────────────────────

    def get_active_session(self, channel: str, chat_id: str) -> dict | None:
        """Return the active session row or None if expired/missing.

        Filters out rows whose expires_at <= now (lazy expiry).
        """
        now_iso = self._iso(self._now())
        row = self._conn.execute(
            "SELECT channel, chat_id, base_user_id, started_at, expires_at "
            "FROM elevation_sessions "
            "WHERE channel = ? AND chat_id = ? AND expires_at > ?",
            (channel, chat_id, now_iso),
        ).fetchone()
        if row is None:
            return None
        return {
            "channel": row[0],
            "chat_id": row[1],
            "base_user_id": row[2],
            "started_at": row[3],
            "expires_at": row[4],
        }

    def elevate(
        self,
        channel: str,
        chat_id: str,
        base_user_id: int,
        ttl_minutes: int | None = None,
    ) -> str:
        """Create or refresh an elevation session. Returns ISO expires_at."""
        ttl = ttl_minutes if ttl_minutes is not None else config.SUDO_TTL_MINUTES
        now = self._now()
        expires_at = now + timedelta(minutes=ttl)
        now_iso = self._iso(now)
        expires_iso = self._iso(expires_at)
        with self._conn:
            self._conn.execute(
                "INSERT INTO elevation_sessions "
                "    (channel, chat_id, base_user_id, started_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(channel, chat_id) DO UPDATE SET "
                "    base_user_id = excluded.base_user_id, "
                "    started_at   = excluded.started_at, "
                "    expires_at   = excluded.expires_at",
                (channel, chat_id, base_user_id, now_iso, expires_iso),
            )
        return expires_iso

    def drop_session(self, channel: str, chat_id: str) -> bool:
        """Remove any session (active or expired) for (channel, chat_id).

        Returns True if a row was deleted.
        """
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM elevation_sessions WHERE channel = ? AND chat_id = ?",
                (channel, chat_id),
            )
        return cur.rowcount > 0

    # ── Rate limiting ────────────────────────────────────────────────────────

    def get_attempts(self, channel: str, chat_id: str) -> dict:
        """Return the rate-limit row for (channel, chat_id), creating defaults."""
        row = self._conn.execute(
            "SELECT failed_count, locked_until, last_attempt_at "
            "FROM sudo_attempts WHERE channel = ? AND chat_id = ?",
            (channel, chat_id),
        ).fetchone()
        if row is None:
            return {"failed_count": 0, "locked_until": None, "last_attempt_at": None}
        return {
            "failed_count": row[0],
            "locked_until": row[1],
            "last_attempt_at": row[2],
        }

    def is_locked(self, channel: str, chat_id: str) -> tuple[bool, str | None]:
        """Return (locked, locked_until_iso). locked=False if no active lock."""
        attempts = self.get_attempts(channel, chat_id)
        locked_until = attempts["locked_until"]
        if not locked_until:
            return False, None
        now_iso = self._iso(self._now())
        if locked_until > now_iso:
            return True, locked_until
        return False, None

    def record_failure(
        self,
        channel: str,
        chat_id: str,
        max_fails: int | None = None,
        lockout_minutes: int | None = None,
    ) -> dict:
        """Increment failed_count. Lock if threshold reached. Returns new state."""
        max_fails = max_fails if max_fails is not None else config.SUDO_MAX_FAILS
        lockout = lockout_minutes if lockout_minutes is not None else config.SUDO_LOCKOUT_MINUTES

        attempts = self.get_attempts(channel, chat_id)
        new_count = attempts["failed_count"] + 1
        now = self._now()
        now_iso = self._iso(now)
        locked_until_iso: str | None = None
        if new_count >= max_fails:
            locked_until_iso = self._iso(now + timedelta(minutes=lockout))

        with self._conn:
            self._conn.execute(
                "INSERT INTO sudo_attempts "
                "    (channel, chat_id, failed_count, locked_until, last_attempt_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(channel, chat_id) DO UPDATE SET "
                "    failed_count    = excluded.failed_count, "
                "    locked_until    = excluded.locked_until, "
                "    last_attempt_at = excluded.last_attempt_at",
                (channel, chat_id, new_count, locked_until_iso, now_iso),
            )
        return {
            "failed_count": new_count,
            "locked_until": locked_until_iso,
            "last_attempt_at": now_iso,
        }

    def reset_failures(self, channel: str, chat_id: str) -> None:
        """Zero the failed_count and clear any lock for (channel, chat_id)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO sudo_attempts "
                "    (channel, chat_id, failed_count, locked_until, last_attempt_at) "
                "VALUES (?, ?, 0, NULL, ?) "
                "ON CONFLICT(channel, chat_id) DO UPDATE SET "
                "    failed_count = 0, "
                "    locked_until = NULL",
                (channel, chat_id, self._iso(self._now())),
            )
