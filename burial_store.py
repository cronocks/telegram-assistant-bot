"""burial_store.py — SQLite-backed burial record CRUD for FR-11.

A member may have multiple records (reburial/relocation history); the newest
record has is_current = 1. Creating a new record demotes the previous current
one automatically.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from db.connection import get_connection


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _validate_gps(lat: float | None, lng: float | None) -> None:
    if (lat is None) != (lng is None):
        raise ValueError("burial: lat and lng must be provided together")
    if lat is not None and not (-90.0 <= lat <= 90.0):
        raise ValueError(f"burial: lat must be -90..90, got {lat}")
    if lng is not None and not (-180.0 <= lng <= 180.0):
        raise ValueError(f"burial: lng must be -180..180, got {lng}")


class SqliteBurialStore:
    """SQLite adapter for the burial_records table."""

    _CREATE_FIELDS = {
        "address", "lat", "lng", "plot_info", "buried_date", "relocated_date",
        "photo_drive_ids", "note",
    }

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Create ────────────────────────────────────────────────────────────────

    def create_record(self, created_by: int, member_id: int, cemetery_name: str, **fields) -> dict:
        if not cemetery_name or not cemetery_name.strip():
            raise ValueError("burial: cemetery_name must be non-empty")
        unknown = set(fields) - self._CREATE_FIELDS
        if unknown:
            raise ValueError(f"burial: unknown fields {sorted(unknown)}")
        _validate_gps(fields.get("lat"), fields.get("lng"))
        member_exists = self._conn.execute(
            "SELECT 1 FROM family_members WHERE id = ?", (member_id,),
        ).fetchone()
        if not member_exists:
            raise ValueError(f"burial: family member {member_id} does not exist")

        now = _utcnow_iso()
        columns = ["member_id", "cemetery_name", "created_by", "created_at", "updated_at"]
        values: list = [member_id, cemetery_name.strip(), created_by, now, now]
        for key in sorted(fields):
            columns.append(key)
            values.append(fields[key])
        placeholders = ", ".join("?" for _ in columns)
        with self._conn:
            # Demote the previous current record (relocation history).
            self._conn.execute(
                "UPDATE burial_records SET is_current = 0, updated_at = ? "
                "WHERE member_id = ? AND is_current = 1 AND deleted_at IS NULL",
                (now, member_id),
            )
            cur = self._conn.execute(
                f"INSERT INTO burial_records ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        return self.get_record(cur.lastrowid)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_record(self, record_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM burial_records WHERE id = ?", (record_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def get_current_for_member(self, member_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM burial_records "
            "WHERE member_id = ? AND is_current = 1 AND deleted_at IS NULL",
            (member_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_for_member(self, member_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM burial_records "
            "WHERE member_id = ? AND deleted_at IS NULL "
            "ORDER BY id DESC",
            (member_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Update ────────────────────────────────────────────────────────────────

    def update_record(self, record_id: int, **fields) -> dict | None:
        allowed = self._CREATE_FIELDS | {"cemetery_name"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_record(record_id)

        current = self.get_record(record_id)
        if current is None:
            return None

        if "cemetery_name" in updates:
            if not updates["cemetery_name"] or not updates["cemetery_name"].strip():
                raise ValueError("burial: cemetery_name must be non-empty")
            updates["cemetery_name"] = updates["cemetery_name"].strip()
        # Validate GPS as a pair against merged values so updating one
        # coordinate keeps consistency with the stored other one.
        merged = {**current, **updates}
        _validate_gps(merged.get("lat"), merged.get("lng"))

        updates["updated_at"] = _utcnow_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [record_id]
        with self._conn:
            self._conn.execute(
                f"UPDATE burial_records SET {set_clause} WHERE id = ?", values,
            )
        return self.get_record(record_id)

    # ── Soft-delete ───────────────────────────────────────────────────────────

    def soft_delete_record(self, record_id: int) -> bool:
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE burial_records SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (now, now, record_id),
            )
        return cur.rowcount > 0
