"""audit.py — SQLite-backed audit log (FR-4).

One row per noteworthy event: stealth-read, sudo, recycle-bin ops, role change,
notification lifecycle, etc. Immutability is enforced at the application layer:
only `SqliteAuditLog.log()` writes; no UPDATE/DELETE methods are exposed.

Payload is an optional dict serialized to JSON. actor_user_id is None for system
events (scheduled jobs, auto-purge). See docs/FR-4-PLAN.md section 2.3 for the
authoritative action taxonomy.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from db.connection import get_connection


@dataclass(frozen=True)
class AuditEvent:
    """One row from the audit_log table, with payload decoded back to a dict."""
    id: int
    actor_user_id: int | None
    action: str
    target_type: str | None
    target_id: str | None
    payload: dict[str, Any] | None
    created_at: str


class SqliteAuditLog:
    """Concrete audit log adapter against a SQLite connection.

    Designed to be the ONLY writer of `audit_log` rows. Other modules call
    `log(...)`; this class never exposes update or delete.
    """

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Write ─────────────────────────────────────────────────────────────────

    def log(
        self,
        actor_user_id: int | None,
        action: str,
        target_type: str | None = None,
        target_id: str | int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Append one event. Returns the new row id.

        - `actor_user_id=None` denotes a system event (e.g. scheduled job).
        - `target_id` is normalized to TEXT so notes (int) and drive_file_id
          (str) share the same column.
        - `payload`, if provided, is serialized to JSON. Non-serializable values
          will raise — callers are expected to pass JSON-compatible dicts.
        """
        if not action:
            raise ValueError("audit.log: action must be a non-empty string")

        target_id_text = None if target_id is None else str(target_id)
        payload_text = None if payload is None else json.dumps(payload, ensure_ascii=False)

        cur = self._conn.execute(
            "INSERT INTO audit_log (actor_user_id, action, target_type, target_id, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor_user_id, action, target_type, target_id_text, payload_text),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    # ── Read ──────────────────────────────────────────────────────────────────

    def list_recent(
        self,
        limit: int = 50,
        offset: int = 0,
        actor_user_id: int | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | int | None = None,
    ) -> list[AuditEvent]:
        """Return events ordered by `created_at DESC, id DESC`.

        All filter args are optional and combine with AND. `target_id` is
        coerced to TEXT to match storage.
        """
        if limit <= 0:
            return []

        where: list[str] = []
        params: list[Any] = []

        if actor_user_id is not None:
            where.append("actor_user_id = ?")
            params.append(actor_user_id)
        if action is not None:
            where.append("action = ?")
            params.append(action)
        if target_type is not None:
            where.append("target_type = ?")
            params.append(target_type)
        if target_id is not None:
            where.append("target_id = ?")
            params.append(str(target_id))

        sql = (
            "SELECT id, actor_user_id, action, target_type, target_id, payload, created_at "
            "FROM audit_log"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, max(0, offset)])

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_event(row: sqlite3.Row | tuple) -> AuditEvent:
        payload_text = row[5]
        if payload_text is None:
            payload = None
        else:
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                # Stored data shouldn't be invalid, but degrade gracefully.
                payload = {"_raw": payload_text}
        return AuditEvent(
            id=int(row[0]),
            actor_user_id=row[1],
            action=row[2],
            target_type=row[3],
            target_id=row[4],
            payload=payload,
            created_at=row[6],
        )

    # Reserved for future helpers (not part of FR-4.1a):
    # def count(self, ...) -> int
    # def stream(self, since: datetime, ...) -> Iterator[AuditEvent]
    # These are intentionally omitted to keep the surface area minimal.

    @staticmethod
    def _utcnow() -> str:
        """Return current UTC time in the same ISO format SQLite uses for CURRENT_TIMESTAMP."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
