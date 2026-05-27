"""Tests for LedgerReports — FR-9."""
import pytest

from budget_store import SqliteBudgetStore
from ledger_reports import LedgerReports
from ledger_store import SqliteLedgerStore

MAY = "2026-05"
TODAY = "2026-05-26 10:00:00"
YESTERDAY = "2026-05-25 09:00:00"
LAST_MONTH = "2026-04-15 10:00:00"


@pytest.fixture()
def stores(db_conn):
    ledger = SqliteLedgerStore(conn=db_conn)
    budget = SqliteBudgetStore(conn=db_conn)
    reports = LedgerReports(ledger_store=ledger, budget_store=budget)
    return ledger, budget, reports


# ── monthly_summary ───────────────────────────────────────────────────────────


def test_monthly_summary_income_expense_savings(stores, member_user):
    ledger, budget, reports = stores
    ledger.add_entry(member_user.id, "income", 5_000_000, TODAY)
    ledger.add_entry(member_user.id, "expense", 2_000_000, TODAY)

    summary = reports.monthly_summary(member_user.id, MAY)
    assert summary["income"] == 5_000_000
    assert summary["expense"] == 2_000_000
    assert summary["savings"] == 3_000_000


def test_monthly_summary_no_budget_returns_none_pct(stores, member_user):
    _, _, reports = stores
    summary = reports.monthly_summary(member_user.id, MAY)
    assert summary["budget_pct"] is None
    assert summary["expense_budget"] is None


def test_monthly_summary_with_budget(stores, member_user):
    ledger, budget, reports = stores
    ledger.add_entry(member_user.id, "expense", 6_200_000, TODAY)
    budget.upsert_budget(member_user.id, MAY, expense_budget=10_000_000)

    summary = reports.monthly_summary(member_user.id, MAY)
    assert summary["expense_budget"] == 10_000_000
    assert summary["budget_pct"] == 62


def test_monthly_summary_by_category(stores, member_user, db_conn):
    ledger, _, reports = stores
    db_conn.execute(
        "INSERT INTO categories (user_id, name, kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (member_user.id, "Ăn uống", "expense", TODAY, TODAY),
    )
    cat_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    ledger.add_entry(member_user.id, "expense", 500_000, TODAY, category_id=cat_id)
    ledger.add_entry(member_user.id, "expense", 300_000, TODAY, category_id=cat_id)

    summary = reports.monthly_summary(member_user.id, MAY)
    row = next(r for r in summary["by_category"] if r["category_id"] == cat_id)
    assert row["total"] == 800_000


def test_monthly_summary_empty_month_returns_zeros(stores, member_user):
    _, _, reports = stores
    summary = reports.monthly_summary(member_user.id, MAY)
    assert summary["income"] == 0
    assert summary["expense"] == 0
    assert summary["savings"] == 0


# ── yearly_breakdown ──────────────────────────────────────────────────────────


def test_yearly_breakdown_returns_12_months(stores, member_user):
    _, _, reports = stores
    rows = reports.yearly_breakdown(member_user.id, "2026")
    assert len(rows) == 12
    months = [r["month"] for r in rows]
    assert "2026-01" in months
    assert "2026-12" in months


def test_yearly_breakdown_correct_totals(stores, member_user):
    ledger, _, reports = stores
    ledger.add_entry(member_user.id, "income", 5_000_000, TODAY)       # May
    ledger.add_entry(member_user.id, "expense", 2_000_000, TODAY)      # May
    ledger.add_entry(member_user.id, "expense", 1_000_000, LAST_MONTH) # April

    rows = reports.yearly_breakdown(member_user.id, "2026")
    may_row = next(r for r in rows if r["month"] == "2026-05")
    apr_row = next(r for r in rows if r["month"] == "2026-04")

    assert may_row["income"] == 5_000_000
    assert may_row["expense"] == 2_000_000
    assert apr_row["expense"] == 1_000_000


# ── last_7_days ───────────────────────────────────────────────────────────────


def test_last_7_days_sums_entries(stores, member_user):
    ledger, _, reports = stores
    ledger.add_entry(member_user.id, "expense", 100_000, TODAY)
    ledger.add_entry(member_user.id, "expense", 50_000, YESTERDAY)

    result = reports.last_7_days(member_user.id, since=YESTERDAY)
    assert result["total_expense"] == 150_000
    assert result["total_income"] == 0
    assert len(result["entries"]) == 2


def test_last_7_days_excludes_entries_before_since(stores, member_user):
    ledger, _, reports = stores
    ledger.add_entry(member_user.id, "expense", 200_000, LAST_MONTH)
    ledger.add_entry(member_user.id, "expense", 100_000, TODAY)

    result = reports.last_7_days(member_user.id, since=YESTERDAY)
    assert result["total_expense"] == 100_000


# ── check_threshold ───────────────────────────────────────────────────────────


def test_check_threshold_no_budget_returns_none(stores, member_user):
    ledger, _, reports = stores
    ledger.add_entry(member_user.id, "expense", 5_000_000, TODAY)
    assert reports.check_threshold(member_user.id, MAY) is None


def test_check_threshold_below_80_returns_none(stores, member_user):
    ledger, budget, reports = stores
    budget.upsert_budget(member_user.id, MAY, expense_budget=10_000_000)
    ledger.add_entry(member_user.id, "expense", 7_000_000, TODAY)  # 70%
    assert reports.check_threshold(member_user.id, MAY) is None


def test_check_threshold_at_80_returns_80(stores, member_user):
    ledger, budget, reports = stores
    budget.upsert_budget(member_user.id, MAY, expense_budget=10_000_000)
    ledger.add_entry(member_user.id, "expense", 8_000_000, TODAY)  # 80%
    assert reports.check_threshold(member_user.id, MAY) == "80"


def test_check_threshold_at_100_returns_100(stores, member_user):
    ledger, budget, reports = stores
    budget.upsert_budget(member_user.id, MAY, expense_budget=10_000_000)
    ledger.add_entry(member_user.id, "expense", 10_000_000, TODAY)  # 100%
    assert reports.check_threshold(member_user.id, MAY) == "100"


def test_check_threshold_already_sent_returns_none(stores, member_user):
    ledger, budget, reports = stores
    budget.upsert_budget(member_user.id, MAY, expense_budget=10_000_000)
    ledger.add_entry(member_user.id, "expense", 9_000_000, TODAY)  # 90%
    budget.mark_alert_sent(member_user.id, MAY, "80")
    assert reports.check_threshold(member_user.id, MAY) is None
