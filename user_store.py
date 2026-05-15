"""user_store.py — SQLite-backed user registry."""
from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone

import config
from db.connection import get_connection
from interfaces import User

logger = logging.getLogger(__name__)


class SqliteUserStore:
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        # Accept an injected connection (useful for tests with in-memory DB).
        self._conn = conn or get_connection()

    # ── User queries ──────────────────────────────────────────────────────────

    def get_user_by_id(self, user_id: int) -> User | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _row_to_user(row) if row else None

    def list_users(self, include_deleted: bool = False) -> list[User]:
        if include_deleted:
            rows = self._conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE deleted_at IS NULL ORDER BY id"
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def find_by_channel(self, channel: str, chat_id: str) -> User | None:
        """Return the active user bound to (channel, chat_id), or None."""
        row = self._conn.execute(
            """
            SELECT u.* FROM users u
            JOIN channel_bindings cb ON cb.user_id = u.id
            WHERE cb.channel = ? AND cb.chat_id = ?
            """,
            (channel, chat_id),
        ).fetchone()
        return _row_to_user(row) if row else None

    # ── User mutations ────────────────────────────────────────────────────────

    def create_user(
        self,
        name: str,
        role: str,
        birthdate: date | None = None,
        username: str | None = None,
    ) -> User:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO users (name, role, birthdate, username)
                VALUES (?, ?, ?, ?)
                """,
                (name, role, birthdate.isoformat() if birthdate else None, username),
            )
        user = self.get_user_by_id(cur.lastrowid)
        assert user is not None
        return user

    def soft_delete_user(self, user_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE users SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND deleted_at IS NULL",
                (user_id,),
            )

    def update_user_role(self, user_id: int, role: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE users SET role = ? WHERE id = ? AND deleted_at IS NULL",
                (role, user_id),
            )

    # ── Channel bindings ──────────────────────────────────────────────────────

    def bind_channel(self, user_id: int, channel: str, chat_id: str) -> None:
        """Bind a (channel, chat_id) pair to a user. Raises on duplicate."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO channel_bindings (user_id, channel, chat_id)
                VALUES (?, ?, ?)
                """,
                (user_id, channel, chat_id),
            )

    # ── Invite codes ──────────────────────────────────────────────────────────

    def create_invite_code(
        self,
        intended_user_id: int,
        created_by: int,
        ttl_days: int = 7,
    ) -> str:
        """Generate an 8-char hex invite code valid for ttl_days days."""
        code = secrets.token_hex(4)  # 8 hex chars
        expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO invite_codes
                    (code, intended_user_id, created_by, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (code, intended_user_id, created_by, expires_at.isoformat()),
            )
        return code

    def consume_invite_code(
        self, code: str, channel: str, chat_id: str
    ) -> User | None:
        """Validate and consume an invite code; bind the channel; return the user.

        Returns None if the code is invalid, expired, or already used.
        """
        row = self._conn.execute(
            """
            SELECT * FROM invite_codes
            WHERE code = ? AND used_at IS NULL AND expires_at > CURRENT_TIMESTAMP
            """,
            (code,),
        ).fetchone()
        if not row:
            return None

        user_id = row["intended_user_id"]
        now = datetime.now(timezone.utc).isoformat()

        with self._conn:
            self._conn.execute(
                """
                UPDATE invite_codes
                SET used_at = ?, used_channel = ?, used_chat_id = ?
                WHERE code = ?
                """,
                (now, channel, chat_id, code),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO channel_bindings (user_id, channel, chat_id)
                VALUES (?, ?, ?)
                """,
                (user_id, channel, chat_id),
            )

        return self.get_user_by_id(user_id)

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    # ── Birthdate changes ─────────────────────────────────────────────────────

    def request_birthdate_change(self, user_id: int, new_birthdate: date) -> int:
        """Create a pending birthdate change request. Returns the request id.

        Raises ValueError if there is already a pending request for this user.
        """
        existing = self.get_pending_birthdate_change(user_id)
        if existing is not None:
            raise ValueError(f"User {user_id} already has a pending birthdate request (id={existing['id']})")

        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO birthdate_changes (user_id, new_birthdate)
                VALUES (?, ?)
                """,
                (user_id, new_birthdate.isoformat()),
            )
        return cur.lastrowid

    def get_pending_birthdate_change(self, user_id: int) -> dict | None:
        """Return the pending birthdate change row for a user, or None."""
        row = self._conn.execute(
            """
            SELECT * FROM birthdate_changes
            WHERE user_id = ? AND approved_at IS NULL AND rejected_at IS NULL
            ORDER BY requested_at DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_pending_birthdate_changes(self) -> list[dict]:
        """Return all pending birthdate change requests."""
        rows = self._conn.execute(
            """
            SELECT bc.*, u.name as user_name
            FROM birthdate_changes bc
            JOIN users u ON u.id = bc.user_id
            WHERE bc.approved_at IS NULL AND bc.rejected_at IS NULL
            ORDER BY bc.requested_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def approve_birthdate_change(self, request_id: int, approver_id: int) -> bool:
        """Approve a birthdate change request and update the user's birthdate.

        Returns True if approved, False if request not found or already resolved.
        """
        row = self._conn.execute(
            "SELECT * FROM birthdate_changes WHERE id = ? AND approved_at IS NULL AND rejected_at IS NULL",
            (request_id,),
        ).fetchone()
        if row is None:
            return False

        with self._conn:
            self._conn.execute(
                """
                UPDATE birthdate_changes
                SET approved_by = ?, approved_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (approver_id, request_id),
            )
            self._conn.execute(
                "UPDATE users SET birthdate = ? WHERE id = ?",
                (row["new_birthdate"], row["user_id"]),
            )
        return True

    def reject_birthdate_change(
        self, request_id: int, approver_id: int, note: str = ""
    ) -> bool:
        """Reject a birthdate change request.

        Returns True if rejected, False if request not found or already resolved.
        """
        row = self._conn.execute(
            "SELECT id FROM birthdate_changes WHERE id = ? AND approved_at IS NULL AND rejected_at IS NULL",
            (request_id,),
        ).fetchone()
        if row is None:
            return False

        with self._conn:
            self._conn.execute(
                """
                UPDATE birthdate_changes
                SET approved_by = ?, rejected_at = CURRENT_TIMESTAMP, rejection_note = ?
                WHERE id = ?
                """,
                (approver_id, note, request_id),
            )
        return True

    # ── Username changes ──────────────────────────────────────────────────────

    def set_username_direct(self, user_id: int, username: str) -> None:
        """Set username directly (first-set only: current username must be NULL).

        Raises ValueError if user already has a username.
        """
        user = self.get_user_by_id(user_id)
        if user is None:
            raise ValueError(f"User {user_id} not found")
        if user.username is not None:
            raise ValueError(
                f"User {user_id} already has username '{user.username}'. "
                "Use request_username_change to change it."
            )
        with self._conn:
            self._conn.execute(
                "UPDATE users SET username = ? WHERE id = ?", (username, user_id)
            )

    def request_username_change(self, user_id: int, new_username: str) -> int:
        """Queue a username change request (requires admin approval).

        Raises ValueError if there is already a pending request, or the
        rate-limit (30 days since last approved change) has not elapsed.
        Returns the new request id.
        """
        existing = self.get_pending_username_change(user_id)
        if existing is not None:
            raise ValueError(f"User {user_id} already has a pending username request (id={existing['id']})")

        last_row = self._conn.execute(
            "SELECT MAX(approved_at) as last_approved FROM username_changes WHERE user_id = ? AND approved_at IS NOT NULL",
            (user_id,),
        ).fetchone()["last_approved"]

        if last_row:
            from datetime import timezone as _tz
            last_dt = datetime.fromisoformat(last_row)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=_tz.utc)
            days_since = (datetime.now(_tz.utc) - last_dt).days
            if days_since < 30:
                raise ValueError(
                    f"Username đã được đổi {days_since} ngày trước. "
                    f"Phải chờ {30 - days_since} ngày nữa."
                )

        user = self.get_user_by_id(user_id)
        old_username = user.username if user else None

        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO username_changes (user_id, old_username, new_username) VALUES (?, ?, ?)",
                (user_id, old_username, new_username),
            )
        return cur.lastrowid

    def get_pending_username_change(self, user_id: int) -> dict | None:
        """Return the pending username change row for a user, or None."""
        row = self._conn.execute(
            """
            SELECT * FROM username_changes
            WHERE user_id = ? AND approved_at IS NULL AND rejected_at IS NULL
            ORDER BY requested_at DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_pending_username_changes(self) -> list[dict]:
        """Return all pending username change requests."""
        rows = self._conn.execute(
            """
            SELECT uc.*, u.name as user_name
            FROM username_changes uc
            JOIN users u ON u.id = uc.user_id
            WHERE uc.approved_at IS NULL AND uc.rejected_at IS NULL
            ORDER BY uc.requested_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def approve_username_change(self, request_id: int, approver_id: int) -> bool:
        """Approve a username change and update users.username.

        Returns True if approved, False if not found or already resolved.
        """
        row = self._conn.execute(
            "SELECT * FROM username_changes WHERE id = ? AND approved_at IS NULL AND rejected_at IS NULL",
            (request_id,),
        ).fetchone()
        if row is None:
            return False

        with self._conn:
            self._conn.execute(
                "UPDATE username_changes SET approved_by = ?, approved_at = CURRENT_TIMESTAMP WHERE id = ?",
                (approver_id, request_id),
            )
            self._conn.execute(
                "UPDATE users SET username = ? WHERE id = ?",
                (row["new_username"], row["user_id"]),
            )
        return True

    def reject_username_change(self, request_id: int, approver_id: int, note: str = "") -> bool:
        """Reject a username change request.

        Returns True if rejected, False if not found or already resolved.
        """
        row = self._conn.execute(
            "SELECT id FROM username_changes WHERE id = ? AND approved_at IS NULL AND rejected_at IS NULL",
            (request_id,),
        ).fetchone()
        if row is None:
            return False

        with self._conn:
            self._conn.execute(
                "UPDATE username_changes SET approved_by = ?, rejected_at = CURRENT_TIMESTAMP, rejection_note = ? WHERE id = ?",
                (approver_id, note, request_id),
            )
        return True

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def bootstrap_admin(self) -> User | None:
        """Create the first admin user and bind TELEGRAM_CHAT_ID if users table is empty.

        Returns the existing or newly created admin User, or None if
        TELEGRAM_CHAT_ID is not set.
        """
        count = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count > 0:
            logger.info("bootstrap_admin: users table not empty, skipping")
            return self.list_users()[0]

        if not config.TELEGRAM_CHAT_ID:
            logger.warning("bootstrap_admin: TELEGRAM_CHAT_ID not set, cannot bootstrap")
            return None

        admin = self.create_user(name="Bot Owner", role="admin")

        # Bind telegram channel so the owner can use the bot immediately.
        try:
            self.bind_channel(admin.id, "telegram", str(config.TELEGRAM_CHAT_ID))
            logger.info(
                "bootstrap_admin: bound telegram chat_id=%s to admin id=%s",
                config.TELEGRAM_CHAT_ID,
                admin.id,
            )
        except Exception as e:
            logger.warning("bootstrap_admin: could not bind channel: %s", e)

        logger.info("bootstrap_admin: created admin user id=%s", admin.id)
        return admin


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        name=row["name"],
        role=row["role"],
        birthdate=date.fromisoformat(row["birthdate"]) if row["birthdate"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        deleted_at=datetime.fromisoformat(row["deleted_at"]) if row["deleted_at"] else None,
    )
