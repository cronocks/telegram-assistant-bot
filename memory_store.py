"""memory_store.py — SQLite-backed L1 memory store.

Each user has two rows in `user_memory`:
  kind='memory'  — rolling facts/preferences curated by LLM (MEMORY.md equivalent)
  kind='user'    — stable user profile snapshot (USER.md equivalent)

Rows are created lazily on first read/write. Content starts empty; populated by
the `cap nhat tri nho` command which triggers LLM curation.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from db.connection import get_connection


class SqliteMemoryStore:
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, user_id: int, kind: str) -> str:
        """Return the content for (user_id, kind), or '' if no row yet."""
        row = self._conn.execute(
            "SELECT content FROM user_memory WHERE user_id = ? AND kind = ?",
            (user_id, kind),
        ).fetchone()
        return row[0] if row else ""

    def get_meta(self, user_id: int, kind: str) -> dict | None:
        """Return full metadata row or None."""
        row = self._conn.execute(
            "SELECT user_id, kind, content, updated_at, curated_at "
            "FROM user_memory WHERE user_id = ? AND kind = ?",
            (user_id, kind),
        ).fetchone()
        if row is None:
            return None
        return {
            "user_id": row[0],
            "kind": row[1],
            "content": row[2],
            "updated_at": row[3],
            "curated_at": row[4],
        }

    # ── Write ─────────────────────────────────────────────────────────────────

    def set(self, user_id: int, kind: str, content: str, mark_curated: bool = False) -> None:
        """Upsert content for (user_id, kind). Optionally stamp curated_at."""
        now = self._now()
        curated_at = now if mark_curated else None

        existing = self._conn.execute(
            "SELECT 1 FROM user_memory WHERE user_id = ? AND kind = ?",
            (user_id, kind),
        ).fetchone()

        if existing:
            if mark_curated:
                self._conn.execute(
                    "UPDATE user_memory SET content = ?, updated_at = ?, curated_at = ? "
                    "WHERE user_id = ? AND kind = ?",
                    (content, now, curated_at, user_id, kind),
                )
            else:
                self._conn.execute(
                    "UPDATE user_memory SET content = ?, updated_at = ? "
                    "WHERE user_id = ? AND kind = ?",
                    (content, now, user_id, kind),
                )
        else:
            self._conn.execute(
                "INSERT INTO user_memory (user_id, kind, content, updated_at, curated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, kind, content, now, curated_at),
            )
        self._conn.commit()
