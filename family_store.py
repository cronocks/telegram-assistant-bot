"""family_store.py — SQLite-backed family member CRUD for FR-11.

Stores raw lunar/solar partial dates (year only, or full date, approximate
flag). No calendar conversion here — display shows the stored date as-is;
reminder computation (FR-8 integration, Phase B) converts at runtime.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from db.connection import get_connection
from text_utils import normalize_vn

VALID_DATE_TYPES = {"lunar", "solar"}
VALID_GENDERS = {"nam", "nu"}

_DATE_PREFIXES = ("birth", "death")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _validate_partial_date(prefix: str, fields: dict) -> None:
    """Validate one partial date group (birth_* or death_*).

    Rules: month requires year; day requires month; month 1..12; day 1..30
    (lunar months have at most 30 days; solar day-in-month precision is not
    enforced for historical records).
    """
    date_type = fields.get(f"{prefix}_date_type")
    year = fields.get(f"{prefix}_year")
    month = fields.get(f"{prefix}_month")
    day = fields.get(f"{prefix}_day")

    if date_type is not None and date_type not in VALID_DATE_TYPES:
        raise ValueError(f"family: {prefix}_date_type must be lunar|solar, got {date_type}")
    if month is not None and year is None:
        raise ValueError(f"family: {prefix}_month requires {prefix}_year")
    if day is not None and month is None:
        raise ValueError(f"family: {prefix}_day requires {prefix}_month")
    if month is not None and not (1 <= month <= 12):
        raise ValueError(f"family: {prefix}_month must be 1..12, got {month}")
    if day is not None and not (1 <= day <= 31):
        raise ValueError(f"family: {prefix}_day must be 1..31, got {day}")


class SqliteFamilyStore:
    """SQLite adapter for the family_members table."""

    _CREATE_FIELDS = {
        "alias_name", "gender", "generation", "branch",
        "birth_date_type", "birth_year", "birth_month", "birth_day",
        "birth_leap", "birth_approx",
        "death_date_type", "death_year", "death_month", "death_day",
        "death_leap", "death_approx",
        "bio", "photo_drive_id", "linked_user_id",
    }

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Create ────────────────────────────────────────────────────────────────

    def create_member(self, created_by: int, full_name: str, **fields) -> dict:
        if not full_name or not full_name.strip():
            raise ValueError("family: full_name must be non-empty")
        unknown = set(fields) - self._CREATE_FIELDS
        if unknown:
            raise ValueError(f"family: unknown fields {sorted(unknown)}")
        gender = fields.get("gender")
        if gender is not None and gender not in VALID_GENDERS:
            raise ValueError(f"family: gender must be nam|nu, got {gender}")
        for prefix in _DATE_PREFIXES:
            _validate_partial_date(prefix, fields)

        now = _utcnow_iso()
        columns = ["full_name", "created_by", "created_at", "updated_at"]
        values = [full_name.strip(), created_by, now, now]
        for key in sorted(fields):
            columns.append(key)
            values.append(fields[key])
        placeholders = ", ".join("?" for _ in columns)
        with self._conn:
            cur = self._conn.execute(
                f"INSERT INTO family_members ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        return self.get_member(cur.lastrowid)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_member(self, member_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM family_members WHERE id = ?", (member_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_members(self, generation: int | None = None) -> list[dict]:
        conditions = ["deleted_at IS NULL"]
        params: list = []
        if generation is not None:
            conditions.append("generation = ?")
            params.append(generation)
        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT * FROM family_members WHERE {where} "
            "ORDER BY generation ASC, full_name ASC",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def search_by_name(self, query: str) -> list[dict]:
        """Diacritic-insensitive substring match on full_name and alias_name.

        Filtering happens in Python — SQLite has no Vietnamese collation and
        family-scale data (hundreds of rows) makes a full scan negligible.
        """
        needle = normalize_vn(query).lower().strip()
        if not needle:
            return []
        results = []
        for row in self.list_members():
            haystack = normalize_vn(row["full_name"]).lower()
            alias = row.get("alias_name")
            if alias:
                haystack += " " + normalize_vn(alias).lower()
            if needle in haystack:
                results.append(row)
        return results

    # ── Update ────────────────────────────────────────────────────────────────

    def update_member(self, member_id: int, **fields) -> dict | None:
        allowed = self._CREATE_FIELDS | {"full_name"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_member(member_id)

        current = self.get_member(member_id)
        if current is None:
            return None

        if "full_name" in updates:
            if not updates["full_name"] or not updates["full_name"].strip():
                raise ValueError("family: full_name must be non-empty")
            updates["full_name"] = updates["full_name"].strip()
        gender = updates.get("gender")
        if gender is not None and gender not in VALID_GENDERS:
            raise ValueError(f"family: gender must be nam|nu, got {gender}")
        # Validate date groups against merged (current + updated) values so a
        # partial update (e.g. day only) is checked against the stored month/year.
        merged = {**current, **updates}
        for prefix in _DATE_PREFIXES:
            _validate_partial_date(prefix, merged)

        updates["updated_at"] = _utcnow_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [member_id]
        with self._conn:
            self._conn.execute(
                f"UPDATE family_members SET {set_clause} WHERE id = ?", values,
            )
        return self.get_member(member_id)

    # ── Soft-delete ───────────────────────────────────────────────────────────

    def soft_delete_member(self, member_id: int) -> bool:
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE family_members SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (now, now, member_id),
            )
        return cur.rowcount > 0

    def restore_member(self, member_id: int) -> bool:
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE family_members SET deleted_at = NULL, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NOT NULL",
                (now, member_id),
            )
        return cur.rowcount > 0
