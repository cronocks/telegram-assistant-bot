"""ledger_store.py — SQLite-backed ledger entry CRUD for FR-9 (Expense Tracking)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from db.connection import get_connection

VALID_KINDS = {"income", "expense", "cc_payment"}
VALID_SOURCES = {"telegram", "web"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class SqliteLedgerStore:
    """SQLite adapter for the ledger_entries table."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_connection()

    # ── Create ────────────────────────────────────────────────────────────────

    def add_entry(
        self,
        user_id: int,
        kind: str,
        amount: int,
        occurred_at: str,
        *,
        category_id: int | None = None,
        note: str | None = None,
        source: str = "telegram",
        credit_card_id: int | None = None,
    ) -> dict:
        if kind not in VALID_KINDS:
            raise ValueError(f"ledger: kind must be income|expense|cc_payment, got {kind!r}")
        if amount <= 0:
            raise ValueError(f"ledger: amount must be positive, got {amount}")

        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO ledger_entries
                    (user_id, kind, amount, category_id, note, occurred_at, source,
                     created_at, updated_at, credit_card_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, kind, amount, category_id, note, occurred_at, source,
                 now, now, credit_card_id),
            )
        return self.get_entry(cur.lastrowid)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_entry(self, entry_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM ledger_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def list_for_user(
        self,
        user_id: int,
        *,
        month: str | None = None,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        conditions = ["user_id = ?", "voided_at IS NULL"]
        params: list = [user_id]
        if month is not None:
            conditions.append("occurred_at LIKE ?")
            params.append(f"{month}%")
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        where = " AND ".join(conditions)
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM ledger_entries WHERE {where} ORDER BY occurred_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_last_7_days(self, user_id: int, since: str) -> list[dict]:
        """Entries from `since` to now, non-voided, ordered by occurred_at ASC."""
        rows = self._conn.execute(
            """
            SELECT * FROM ledger_entries
            WHERE user_id = ? AND occurred_at >= ? AND voided_at IS NULL
            ORDER BY occurred_at ASC
            """,
            (user_id, since),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Update ────────────────────────────────────────────────────────────────

    def update_entry(self, entry_id: int, **fields) -> dict | None:
        allowed = {"kind", "amount", "category_id", "note", "occurred_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_entry(entry_id)

        if "kind" in updates and updates["kind"] not in VALID_KINDS:
            raise ValueError(f"ledger: kind must be income|expense, got {updates['kind']!r}")
        if "amount" in updates and updates["amount"] <= 0:
            raise ValueError(f"ledger: amount must be positive, got {updates['amount']}")

        updates["updated_at"] = _utcnow_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [entry_id]
        with self._conn:
            self._conn.execute(
                f"UPDATE ledger_entries SET {set_clause} WHERE id = ?", values
            )
        return self.get_entry(entry_id)

    # ── Soft-delete ───────────────────────────────────────────────────────────

    def purge_voided_older_than(self, threshold_iso: str) -> int:
        """Hard-delete voided entries whose voided_at is older than threshold_iso.

        Returns the number of rows deleted.
        """
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM ledger_entries WHERE voided_at IS NOT NULL AND voided_at < ?",
                (threshold_iso,),
            )
        return cur.rowcount

    def void_entry(self, entry_id: int) -> bool:
        now = _utcnow_iso()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE ledger_entries SET voided_at = ?, updated_at = ? "
                "WHERE id = ? AND voided_at IS NULL",
                (now, now, entry_id),
            )
        return cur.rowcount > 0

    # ── Aggregates ────────────────────────────────────────────────────────────

    def monthly_totals(self, user_id: int, month: str) -> dict:
        """Return {income, expense} sums for the given YYYY-MM month."""
        row = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM ledger_entries
            WHERE user_id = ? AND occurred_at LIKE ? AND voided_at IS NULL
            """,
            (user_id, f"{month}%"),
        ).fetchone()
        return {"income": row["income"], "expense": row["expense"]}

    def card_outstanding(self, user_id: int, card_id: int) -> int:
        """Outstanding balance on a card = charged expenses - statement payments.

        Non-voided entries only.
        """
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN kind = 'expense' THEN amount
                     WHEN kind = 'cc_payment' THEN -amount
                     ELSE 0 END
            ), 0) AS outstanding
            FROM ledger_entries
            WHERE user_id = ? AND credit_card_id = ? AND voided_at IS NULL
            """,
            (user_id, card_id),
        ).fetchone()
        return row["outstanding"]

    def all_card_outstanding(self, user_id: int) -> dict:
        """Return {credit_card_id: outstanding} for every card with activity."""
        rows = self._conn.execute(
            """
            SELECT credit_card_id, COALESCE(SUM(
                CASE WHEN kind = 'expense' THEN amount
                     WHEN kind = 'cc_payment' THEN -amount
                     ELSE 0 END
            ), 0) AS outstanding
            FROM ledger_entries
            WHERE user_id = ? AND credit_card_id IS NOT NULL AND voided_at IS NULL
            GROUP BY credit_card_id
            """,
            (user_id,),
        ).fetchall()
        return {r["credit_card_id"]: r["outstanding"] for r in rows}

    def monthly_by_category(self, user_id: int, month: str) -> list[dict]:
        """Return [{category_id, kind, total}, ...] grouped by category for the month."""
        rows = self._conn.execute(
            """
            SELECT category_id, kind, SUM(amount) AS total
            FROM ledger_entries
            WHERE user_id = ? AND occurred_at LIKE ? AND voided_at IS NULL
            GROUP BY category_id, kind
            ORDER BY total DESC
            """,
            (user_id, f"{month}%"),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
