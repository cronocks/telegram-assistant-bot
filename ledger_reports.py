"""ledger_reports.py — Report aggregation for FR-9 (Expense Tracking).

Public API:
    LedgerReports.monthly_summary(user_id, month) -> dict
    LedgerReports.yearly_breakdown(user_id, year) -> list[dict]
    LedgerReports.last_7_days(user_id, since) -> dict
    LedgerReports.check_threshold(user_id, month) -> str | None
        Returns '80', '100', or None (no alert needed).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from budget_store import SqliteBudgetStore
    from ledger_store import SqliteLedgerStore


class LedgerReports:
    """Aggregation layer on top of LedgerStore + BudgetStore."""

    def __init__(
        self,
        ledger_store: "SqliteLedgerStore",
        budget_store: "SqliteBudgetStore",
    ) -> None:
        self._ledger = ledger_store
        self._budget = budget_store

    # ── Monthly summary ───────────────────────────────────────────────────────

    def monthly_summary(self, user_id: int, month: str) -> dict:
        """Return aggregated monthly data for display.

        Keys: income, expense, savings, expense_budget, savings_target,
              budget_pct (None if no budget), by_category (list[dict]).
        """
        totals = self._ledger.monthly_totals(user_id, month)
        income = totals["income"]
        expense = totals["expense"]
        savings = income - expense

        by_category = self._ledger.monthly_by_category(user_id, month)

        budget_row = self._budget.get_budget(user_id, month)
        expense_budget = budget_row["expense_budget"] if budget_row else None
        savings_target = budget_row["savings_target"] if budget_row else None

        budget_pct: int | None = None
        if expense_budget:
            budget_pct = int(expense / expense_budget * 100)

        return {
            "income": income,
            "expense": expense,
            "savings": savings,
            "expense_budget": expense_budget,
            "savings_target": savings_target,
            "budget_pct": budget_pct,
            "by_category": by_category,
        }

    # ── Yearly breakdown ──────────────────────────────────────────────────────

    def yearly_breakdown(self, user_id: int, year: str) -> list[dict]:
        """Return month-by-month rows for the given year (YYYY).

        Always returns 12 rows (one per month), even if empty.
        Each row: {month, income, expense, savings}.
        """
        rows = []
        for m in range(1, 13):
            month = f"{year}-{m:02d}"
            totals = self._ledger.monthly_totals(user_id, month)
            rows.append({
                "month": month,
                "income": totals["income"],
                "expense": totals["expense"],
                "savings": totals["income"] - totals["expense"],
            })
        return rows

    # ── Last 7 days ───────────────────────────────────────────────────────────

    def last_7_days(self, user_id: int, since: str) -> dict:
        """Entries from `since` ISO string onwards.

        Keys: entries (list), total_expense, total_income.
        """
        entries = self._ledger.list_last_7_days(user_id, since)
        total_expense = sum(e["amount"] for e in entries if e["kind"] == "expense")
        total_income = sum(e["amount"] for e in entries if e["kind"] == "income")
        return {
            "entries": entries,
            "total_expense": total_expense,
            "total_income": total_income,
        }

    # ── Threshold check ───────────────────────────────────────────────────────

    def check_threshold(self, user_id: int, month: str) -> str | None:
        """Check if a threshold alert should fire.

        Returns '100', '80', or None. Does NOT mutate alerts_sent.
        """
        budget_row = self._budget.get_budget(user_id, month)
        if budget_row is None or not budget_row["expense_budget"]:
            return None

        totals = self._ledger.monthly_totals(user_id, month)
        expense = totals["expense"]
        budget = budget_row["expense_budget"]
        ratio = expense / budget

        if ratio >= 1.0 and not self._budget.is_alert_sent(user_id, month, "100"):
            return "100"
        if ratio >= 0.8 and not self._budget.is_alert_sent(user_id, month, "80"):
            return "80"
        return None
