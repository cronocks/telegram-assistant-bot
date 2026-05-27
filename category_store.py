"""category_store.py — SQLite-backed category CRUD for FR-9 (Expense Tracking)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from db.connection import get_connection

VALID_KINDS = {"income", "expense"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class SqliteCategoryStore:
    """SQLite adapter for the categories table."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Create ────────────────────────────────────────────────────────────────

    def create_category(
        self,
        name: str,
        kind: str,
        *,
        user_id: int | None = None,
        parent_id: int | None = None,
    ) -> dict:
        if not name or not name.strip():
            raise ValueError("category: name must be non-empty")
        if kind not in VALID_KINDS:
            raise ValueError(f"category: kind must be income|expense, got {kind!r}")

        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO categories (user_id, name, kind, parent_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, name.strip(), kind, parent_id, now, now),
            )
        return self.get_category(cur.lastrowid)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_category(self, category_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM categories WHERE id = ?", (category_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_for_user(
        self,
        user_id: int,
        *,
        kind: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict]:
        """Return categories owned by user_id plus all family-shared (user_id IS NULL)."""
        conditions = ["(user_id = ? OR user_id IS NULL)"]
        params: list = [user_id]
        if not include_deleted:
            conditions.append("deleted_at IS NULL")
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT * FROM categories WHERE {where} ORDER BY kind ASC, name ASC",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Update ────────────────────────────────────────────────────────────────

    def update_category(self, category_id: int, **fields) -> dict | None:
        allowed = {"name", "kind", "parent_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_category(category_id)

        if "name" in updates:
            if not updates["name"] or not updates["name"].strip():
                raise ValueError("category: name must be non-empty")
            updates["name"] = updates["name"].strip()
        if "kind" in updates and updates["kind"] not in VALID_KINDS:
            raise ValueError(f"category: kind must be income|expense, got {updates['kind']!r}")

        updates["updated_at"] = _utcnow_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [category_id]
        with self._conn:
            self._conn.execute(
                f"UPDATE categories SET {set_clause} WHERE id = ?", values
            )
        return self.get_category(category_id)

    # ── Soft-delete ───────────────────────────────────────────────────────────

    def soft_delete_category(self, category_id: int) -> bool:
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE categories SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (now, now, category_id),
            )
        return cur.rowcount > 0
