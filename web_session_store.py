"""web_session_store.py — SQLite-backed web session store (FR-5).

Sessions are server-side revocable: logout sets revoked_at so that stolen
cookies can be invalidated without waiting for TTL expiry.

Token entropy: 32 random bytes → 64-char hex string (256-bit).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from db.connection import get_connection
import config


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class SqliteWebSessionStore:
    """Concrete impl of WebSessionStore Protocol backed by SQLite web_sessions table."""

    def __init__(self, ttl_days: int | None = None) -> None:
        self._conn = get_connection()
        self._ttl_days = ttl_days if ttl_days is not None else config.WEB_SESSION_TTL_DAYS

    # ── Public API ─────────────────────────────────────────────────────────────

    def create(self, user_id: int) -> str:
        """Create a new session for user_id. Returns the session token."""
        token = secrets.token_hex(32)
        now = _now_utc()
        expires_at = now + timedelta(days=self._ttl_days)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO web_sessions (user_id, token, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, token, now.isoformat(), expires_at.isoformat()),
            )
        return token

    def find_active(self, token: str) -> int | None:
        """Return user_id for a valid (non-expired, non-revoked) token, or None."""
        now = _now_utc().isoformat()
        row = self._conn.execute(
            """
            SELECT user_id FROM web_sessions
            WHERE token = ?
              AND revoked_at IS NULL
              AND expires_at > ?
            """,
            (token, now),
        ).fetchone()
        return row["user_id"] if row else None

    def revoke(self, token: str) -> bool:
        """Mark a session as revoked. Returns True if the token existed."""
        now = _now_utc().isoformat()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE token = ? AND revoked_at IS NULL",
                (now, token),
            )
        return cur.rowcount > 0

    def revoke_all_for_user(self, user_id: int) -> int:
        """Revoke all active sessions for a user. Returns count revoked."""
        now = _now_utc().isoformat()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE web_sessions SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (now, user_id),
            )
        return cur.rowcount
