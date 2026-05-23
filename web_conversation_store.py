"""web_conversation_store.py — SQLite-backed web conversation + message store (FR-5.5).

Conversations are lazy-created: a row is only inserted when the user sends
their first message. SSE queues are keyed by conversation_id (not user_id) so
multi-tab users receive replies in the correct tab.

Thread-safety: inherits from the shared SQLite connection (WAL mode, check_same_thread=False).
"""
from __future__ import annotations

from datetime import datetime, timezone

from db.connection import get_connection


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape_like(query: str) -> str:
    """Escape LIKE metacharacters so user input is treated as literal text."""
    return query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


class SqliteWebConversationStore:
    """Concrete impl of WebConversationStore Protocol backed by SQLite."""

    def __init__(self) -> None:
        self._conn = get_connection()

    # ── Conversation CRUD ──────────────────────────────────────────────────────

    def create(self, user_id: int) -> int:
        """Create an empty conversation for user_id. Returns conversation id."""
        now = _now_utc()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO web_conversations (user_id, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (user_id, now, now),
            )
        return cur.lastrowid

    def get(self, conv_id: int) -> dict | None:
        """Return {id, user_id, title, created_at, updated_at} or None."""
        row = self._conn.execute(
            "SELECT id, user_id, title, created_at, updated_at FROM web_conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_for_user(self, user_id: int) -> list[dict]:
        """Return all conversations for user ordered by updated_at DESC."""
        rows = self._conn.execute(
            """
            SELECT id, user_id, title, created_at, updated_at
            FROM web_conversations
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def rename(self, conv_id: int, new_title: str) -> bool:
        """Update title. Returns True if the conversation exists."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE web_conversations SET title = ? WHERE id = ?",
                (new_title.strip(), conv_id),
            )
        return cur.rowcount > 0

    def set_title_if_null(self, conv_id: int, title: str) -> bool:
        """Set title only when currently NULL (idempotent for async title gen).

        Returns True if the title was actually written (was NULL before).
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE web_conversations SET title = ? WHERE id = ? AND title IS NULL",
                (title.strip(), conv_id),
            )
        return cur.rowcount > 0

    # ── Messages ───────────────────────────────────────────────────────────────

    def add_message(self, conv_id: int, role: str, text: str) -> int:
        """Insert a message and bump conversation.updated_at. Returns message id."""
        now = _now_utc()
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO web_messages (conversation_id, role, text, created_at) VALUES (?, ?, ?, ?)",
                (conv_id, role, text, now),
            )
            self._conn.execute(
                "UPDATE web_conversations SET updated_at = ? WHERE id = ?",
                (now, conv_id),
            )
        return cur.lastrowid

    def list_messages(self, conv_id: int) -> list[dict]:
        """Return [{id, role, text, created_at}] in chronological order."""
        rows = self._conn.execute(
            """
            SELECT id, role, text, created_at
            FROM web_messages
            WHERE conversation_id = ?
            ORDER BY created_at, id
            """,
            (conv_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_messages(self, conv_id: int) -> int:
        """Return total message count for a conversation."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM web_messages WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    # ── Search ─────────────────────────────────────────────────────────────────

    def search(self, user_id: int, query: str, limit: int = 50) -> list[dict]:
        """LIKE-based search across messages for a user.

        Returns [{conv_id, conv_title, message_id, role, snippet, created_at}].
        """
        escaped = _escape_like(query.strip())
        pattern = f"%{escaped}%"
        rows = self._conn.execute(
            """
            SELECT
                m.id        AS message_id,
                m.role,
                m.text      AS snippet,
                m.created_at,
                c.id        AS conv_id,
                c.title     AS conv_title
            FROM web_messages m
            JOIN web_conversations c ON c.id = m.conversation_id
            WHERE c.user_id = ?
              AND m.text LIKE ? ESCAPE '\\'
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (user_id, pattern, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Admin stealth-read ─────────────────────────────────────────────────────

    def admin_list_for_user(self, target_user_id: int) -> list[dict]:
        """Admin stealth-read: list conversations of any user (no ownership check).

        Caller must verify admin role + under-18 status before calling.
        """
        rows = self._conn.execute(
            """
            SELECT id, user_id, title, created_at, updated_at
            FROM web_conversations
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (target_user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
