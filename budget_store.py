"""budget_store.py — SQLite-backed monthly budget CRUD for FR-9 (Expense Tracking)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from db.connection import get_connection


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class SqliteBudgetStore:
    """SQLite adapter for the monthly_budgets table."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Upsert ────────────────────────────────────────────────────────────────

    def upsert_budget(
        self,
        user_id: int,
        month: str,
        *,
        expense_budget: int | None = None,
        savings_target: int | None = None,
    ) -> dict:
        now = _utcnow_iso()
        existing = self.get_budget(user_id, month)
        if existing is None:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO monthly_budgets
                        (user_id, month, expense_budget, savings_target, alerts_sent, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '[]', ?, ?)
                    """,
                    (user_id, month, expense_budget, savings_target, now, now),
                )
        else:
            updates: dict = {"updated_at": now}
            if expense_budget is not None:
                updates["expense_budget"] = expense_budget
            if savings_target is not None:
                updates["savings_target"] = savings_target
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [user_id, month]
            with self._conn:
                self._conn.execute(
                    f"UPDATE monthly_budgets SET {set_clause} WHERE user_id = ? AND month = ?",
                    values,
                )
        return self.get_budget(user_id, month)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_budget(self, user_id: int, month: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM monthly_budgets WHERE user_id = ? AND month = ?",
            (user_id, month),
        ).fetchone()
        return _row_to_dict(row) if row else None

    # ── Threshold alert state ─────────────────────────────────────────────────

    def mark_alert_sent(self, user_id: int, month: str, threshold: str) -> None:
        """Record that the threshold alert (e.g. "80" or "100") was fired this month."""
        row = self.get_budget(user_id, month)
        if row is None:
            return
        alerts: list = json.loads(row["alerts_sent"])
        if threshold not in alerts:
            alerts.append(threshold)
        now = _utcnow_iso()
        with self._conn:
            self._conn.execute(
                "UPDATE monthly_budgets SET alerts_sent = ?, updated_at = ? "
                "WHERE user_id = ? AND month = ?",
                (json.dumps(alerts), now, user_id, month),
            )

    def is_alert_sent(self, user_id: int, month: str, threshold: str) -> bool:
        row = self.get_budget(user_id, month)
        if row is None:
            return False
        alerts: list = json.loads(row["alerts_sent"])
        return threshold in alerts
