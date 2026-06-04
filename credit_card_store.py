"""credit_card_store.py — SQLite-backed credit card CRUD for FR-9 (Expense Tracking).

A credit card is a lightweight account used to separate two things that would
otherwise be double-counted:
  * spending on the card during the month  → an `expense` carrying credit_card_id
  * paying off the statement at cycle end   → a `cc_payment` (NOT an expense)
Outstanding balance = sum(card expenses) - sum(card payments).
"""
from __future__ import annotations

import sqlite3
import unicodedata
from datetime import datetime, timezone

from db.connection import get_connection


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _normalize(text: str) -> str:
    """Lowercase + strip diacritics for forgiving name lookup."""
    nfkd = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


class SqliteCreditCardStore:
    """SQLite adapter for the credit_cards table."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    def create_card(self, name: str, *, user_id: int | None = None) -> dict:
        if not name or not name.strip():
            raise ValueError("credit_card: name must be non-empty")
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO credit_cards (user_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, name.strip(), now, now),
            )
        return self.get_card(cur.lastrowid)

    def get_card(self, card_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM credit_cards WHERE id = ?", (card_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def get_card_by_name(self, user_id: int, name: str) -> dict | None:
        """Resolve a card by name (case/diacritic-insensitive) among the user's
        own cards plus family-shared cards. Returns None if not found."""
        target = _normalize(name)
        for card in self.list_for_user(user_id):
            if _normalize(card["name"]) == target:
                return card
        return None

    def list_for_user(self, user_id: int, *, include_deleted: bool = False) -> list[dict]:
        """Return cards owned by user_id plus all family-shared (user_id IS NULL)."""
        conditions = ["(user_id = ? OR user_id IS NULL)"]
        params: list = [user_id]
        if not include_deleted:
            conditions.append("deleted_at IS NULL")
        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT * FROM credit_cards WHERE {where} ORDER BY name ASC", params
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def soft_delete_card(self, card_id: int) -> bool:
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE credit_cards SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (now, now, card_id),
            )
        return cur.rowcount > 0
