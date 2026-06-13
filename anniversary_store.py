"""anniversary_store.py — SQLite-backed anniversary CRUD for FR-8.

Stores raw lunar/solar month-day; solar date is recomputed each year at runtime
by the AnniversaryEngine (Decision #47). Soft-delete via deleted_at (FR-4 recycle
bin compat).
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime, timezone

from db.connection import get_connection

VALID_DATE_TYPES = {"lunar", "solar"}
VALID_CATEGORIES = {"gio", "cuoi", "khac"}
DEFAULT_OFFSETS = "30,15,7,3,1,0"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _validate_month_day(date_type: str, month: int, day: int) -> None:
    if not (1 <= month <= 12):
        raise ValueError(f"anniversary: month must be 1..12, got {month}")
    max_day = 30 if date_type == "lunar" else calendar.monthrange(2024, month)[1]
    # 2024 is a leap year — allows Feb 29 for solar dates; runtime recompute
    # falls back to Feb 28 in non-leap years.
    if not (1 <= day <= max_day):
        raise ValueError(f"anniversary: day {day} out of range for {date_type} month {month}")


class SqliteAnniversaryStore:
    """SQLite adapter for the anniversaries table."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Create ────────────────────────────────────────────────────────────────

    def create_anniversary(
        self,
        user_id: int,
        name: str,
        date_type: str,
        month: int,
        day: int,
        *,
        year: int | None = None,
        is_leap_month: int = 0,
        category: str = "khac",
        reminder_offsets: str = DEFAULT_OFFSETS,
        enabled: int = 1,
        note: str | None = None,
        family_member_id: int | None = None,
    ) -> dict:
        if not name or not name.strip():
            raise ValueError("anniversary: name must be non-empty")
        if date_type not in VALID_DATE_TYPES:
            raise ValueError(f"anniversary: date_type must be lunar|solar, got {date_type}")
        if category not in VALID_CATEGORIES:
            raise ValueError(f"anniversary: category must be one of {VALID_CATEGORIES}")
        _validate_month_day(date_type, month, day)

        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO anniversaries (
                    user_id, name, date_type, month, day, year, is_leap_month, category,
                    reminder_offsets, enabled, note, family_member_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, name.strip(), date_type, month, day, year, is_leap_month,
                    category, reminder_offsets, enabled, note, family_member_id, now, now,
                ),
            )
        return self.get_anniversary(cur.lastrowid)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_anniversary(self, anniversary_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM anniversaries WHERE id = ?", (anniversary_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_for_user(self, user_id: int, *, include_deleted: bool = False) -> list[dict]:
        conditions = ["user_id = ?"]
        params: list = [user_id]
        if not include_deleted:
            conditions.append("deleted_at IS NULL")
        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT * FROM anniversaries WHERE {where} ORDER BY month ASC, day ASC",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_all_active(self) -> list[dict]:
        """All non-deleted, enabled anniversaries — for annual compute job."""
        rows = self._conn.execute(
            "SELECT * FROM anniversaries WHERE enabled = 1 AND deleted_at IS NULL"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_for_member(self, member_id: int) -> list[dict]:
        """Anniversaries linked to a specific family member."""
        rows = self._conn.execute(
            "SELECT * FROM anniversaries "
            "WHERE family_member_id = ? AND deleted_at IS NULL",
            (member_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Update ────────────────────────────────────────────────────────────────

    def update_anniversary(self, anniversary_id: int, **fields) -> dict | None:
        allowed = {
            "name", "date_type", "month", "day", "year", "is_leap_month", "category",
            "reminder_offsets", "enabled", "note", "family_member_id",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_anniversary(anniversary_id)

        # Validate when changing month/day/date_type — read current row first.
        current = self.get_anniversary(anniversary_id)
        if current is None:
            return None
        dt = updates.get("date_type", current["date_type"])
        m = updates.get("month", current["month"])
        d = updates.get("day", current["day"])
        if dt not in VALID_DATE_TYPES:
            raise ValueError(f"anniversary: date_type must be lunar|solar, got {dt}")
        _validate_month_day(dt, m, d)
        if "category" in updates and updates["category"] not in VALID_CATEGORIES:
            raise ValueError(f"anniversary: invalid category {updates['category']}")

        updates["updated_at"] = _utcnow_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [anniversary_id]
        with self._conn:
            self._conn.execute(
                f"UPDATE anniversaries SET {set_clause} WHERE id = ?", values,
            )
        return self.get_anniversary(anniversary_id)

    # ── Soft-delete ───────────────────────────────────────────────────────────

    def soft_delete_anniversary(self, anniversary_id: int) -> bool:
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE anniversaries SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (now, now, anniversary_id),
            )
        return cur.rowcount > 0

    def restore_anniversary(self, anniversary_id: int) -> bool:
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE anniversaries SET deleted_at = NULL, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NOT NULL",
                (now, anniversary_id),
            )
        return cur.rowcount > 0
