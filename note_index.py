"""note_index.py — SQLite ACL/index layer for Drive-backed notes and wiki pages.

Drive holds file content. This module owns the `notes` and `wiki_pages` tables
which record who created each file (owner_user_id) and who can read it (scope).

Scope values:
    'private'  — readable by the owner only
    'everyone' — readable by all active users

Orphan files (Drive file with no SQLite row) are treated as invisible — safe
default that prevents accidental leakage of pre-index content.
"""
from __future__ import annotations

import logging
import re
import sqlite3

from db.connection import get_connection

logger = logging.getLogger(__name__)

# Pattern that identifies a daily journal file by name.
_JOURNAL_RE = re.compile(r"NhatKy", re.IGNORECASE)


class SqliteNoteIndex:
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Write ─────────────────────────────────────────────────────────────────

    def add_note(
        self,
        drive_file_id: str,
        owner_user_id: int,
        kind: str = "note",
        title: str | None = None,
        scope: str = "private",
    ) -> int:
        """Insert a new note row. Returns the SQLite row id."""
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO notes (drive_file_id, owner_user_id, kind, title, scope)
                VALUES (?, ?, ?, ?, ?)
                """,
                (drive_file_id, owner_user_id, kind, title, scope),
            )
        return cur.lastrowid  # type: ignore[return-value]

    def add_wiki_page(
        self,
        drive_file_id: str,
        owner_user_id: int,
        topic: str,
        slug: str,
        scope: str = "everyone",
    ) -> int:
        """Insert a new wiki_page row. Returns the SQLite row id."""
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO wiki_pages (drive_file_id, owner_user_id, topic, slug, scope)
                VALUES (?, ?, ?, ?, ?)
                """,
                (drive_file_id, owner_user_id, topic, slug, scope),
            )
        return cur.lastrowid  # type: ignore[return-value]

    def touch_note(self, drive_file_id: str) -> None:
        """Bump updated_at for an existing note row (called on append)."""
        with self._conn:
            self._conn.execute(
                "UPDATE notes SET updated_at = STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE drive_file_id = ?",
                (drive_file_id,),
            )

    def touch_wiki_page(self, drive_file_id: str) -> None:
        """Bump updated_at for an existing wiki_page row (called on append)."""
        with self._conn:
            self._conn.execute(
                "UPDATE wiki_pages SET updated_at = STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE drive_file_id = ?",
                (drive_file_id,),
            )

    def set_note_scope(
        self, drive_file_id: str, scope: str, requester_id: int
    ) -> bool:
        """Change note scope. Returns False if requester is not the owner."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE notes SET scope = ?, updated_at = STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE drive_file_id = ? AND owner_user_id = ? AND deleted_at IS NULL",
                (scope, drive_file_id, requester_id),
            )
        return cur.rowcount > 0

    def set_wiki_scope(
        self, drive_file_id: str, scope: str, requester_id: int
    ) -> bool:
        """Change wiki page scope. Returns False if requester is not the owner."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE wiki_pages SET scope = ?, updated_at = STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE drive_file_id = ? AND owner_user_id = ? AND deleted_at IS NULL",
                (scope, drive_file_id, requester_id),
            )
        return cur.rowcount > 0

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_note_meta(self, drive_file_id: str) -> dict | None:
        """Return note metadata dict or None if not found / deleted."""
        row = self._conn.execute(
            "SELECT id, drive_file_id, owner_user_id, scope, kind, title, created_at"
            " FROM notes WHERE drive_file_id = ? AND deleted_at IS NULL",
            (drive_file_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(
            ["id", "drive_file_id", "owner_user_id", "scope", "kind", "title", "created_at"],
            row,
        ))

    def get_wiki_meta(self, drive_file_id: str) -> dict | None:
        """Return wiki_page metadata dict or None if not found / deleted."""
        row = self._conn.execute(
            "SELECT id, drive_file_id, owner_user_id, scope, topic, slug, created_at"
            " FROM wiki_pages WHERE drive_file_id = ? AND deleted_at IS NULL",
            (drive_file_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(
            ["id", "drive_file_id", "owner_user_id", "scope", "topic", "slug", "created_at"],
            row,
        ))

    def note_meta_for_ids(self, drive_file_ids: list[str]) -> list[dict]:
        """Return note metadata rows for the given Drive file IDs.

        Orphans (IDs with no row) are silently omitted — safe default.
        """
        if not drive_file_ids:
            return []
        placeholders = ",".join("?" * len(drive_file_ids))
        rows = self._conn.execute(
            f"SELECT id, drive_file_id, owner_user_id, scope, kind, title"
            f" FROM notes WHERE drive_file_id IN ({placeholders}) AND deleted_at IS NULL",
            drive_file_ids,
        ).fetchall()
        keys = ["id", "drive_file_id", "owner_user_id", "scope", "kind", "title"]
        return [dict(zip(keys, r)) for r in rows]

    def visible_wiki_slugs(self, viewer_id: int) -> set[str]:
        """Return slugs of wiki pages the viewer may read.

        scope='everyone' → visible to all.
        scope='private'  → visible only to the owner.
        """
        rows = self._conn.execute(
            "SELECT slug, scope, owner_user_id FROM wiki_pages WHERE deleted_at IS NULL"
        ).fetchall()
        result: set[str] = set()
        for slug, scope, owner_id in rows:
            if scope == "everyone" or owner_id == viewer_id:
                result.add(slug)
        return result

    # ── Backfill ──────────────────────────────────────────────────────────────

    def backfill(
        self,
        note_files: list[dict],
        wiki_files: list[dict],
        admin_user_id: int,
    ) -> int:
        """Index Drive files that have no SQLite row yet.

        Args:
            note_files:    [{id, name, ...}] from DriveNoteStore.list_recent_files
                           (or equivalent full listing).
            wiki_files:    [{id, name, ...}] from DriveWikiStore.list_pages.
            admin_user_id: Assigned as owner for all backfilled rows.

        Returns the number of rows inserted. Idempotent — safe to call on
        every startup; existing rows are skipped.
        """
        inserted = 0

        # Fetch all already-indexed drive_file_ids in one query each.
        existing_notes: set[str] = {
            r[0] for r in self._conn.execute("SELECT drive_file_id FROM notes").fetchall()
        }
        existing_wiki: set[str] = {
            r[0] for r in self._conn.execute("SELECT drive_file_id FROM wiki_pages").fetchall()
        }

        with self._conn:
            for f in note_files:
                fid = f.get("id") or f.get("drive_file_id")
                name = f.get("name", "")
                if not fid or fid in existing_notes:
                    continue
                kind = "journal" if _JOURNAL_RE.search(name) else "note"
                self._conn.execute(
                    "INSERT INTO notes (drive_file_id, owner_user_id, kind, title, scope)"
                    " VALUES (?, ?, ?, ?, 'private')",
                    (fid, admin_user_id, kind, name or None),
                )
                inserted += 1

            for f in wiki_files:
                fid = f.get("id") or f.get("drive_file_id")
                name = f.get("name", "")
                if not fid or fid in existing_wiki:
                    continue
                # Derive topic + slug from filename (strip .md, replace _ with space).
                slug = name.removesuffix(".md")
                topic = slug.replace("_", " ")
                self._conn.execute(
                    "INSERT INTO wiki_pages (drive_file_id, owner_user_id, topic, slug, scope)"
                    " VALUES (?, ?, ?, ?, 'everyone')",
                    (fid, admin_user_id, topic, slug),
                )
                inserted += 1

        if inserted:
            logger.info("note_index backfill: inserted %d rows", inserted)
        return inserted
