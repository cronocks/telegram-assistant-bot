"""user_store.py — SQLite-backed user registry (partial: user CRUD + bootstrap).

Channel bindings and invite codes are added in the next commit.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime

import config
from db.connection import get_connection
from interfaces import User

logger = logging.getLogger(__name__)


class SqliteUserStore:
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        # Accept an injected connection (useful for tests with in-memory DB).
        self._conn = conn or get_connection()

    # ── Queries ───────────────────────────────────────────────────────────────

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

    # ── Mutations ─────────────────────────────────────────────────────────────

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

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def bootstrap_admin(self) -> User | None:
        """Create the first admin user from TELEGRAM_CHAT_ID env var if users table is empty.

        Returns the existing or newly created admin User, or None if TELEGRAM_CHAT_ID
        is not set. Channel binding is wired in the next commit.
        """
        count = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count > 0:
            logger.info("bootstrap_admin: users table not empty, skipping")
            return self.list_users()[0]

        if not config.TELEGRAM_CHAT_ID:
            logger.warning("bootstrap_admin: TELEGRAM_CHAT_ID not set, cannot bootstrap")
            return None

        admin = self.create_user(name="Bot Owner", role="admin")
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
