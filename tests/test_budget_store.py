"""Tests for SqliteBudgetStore — FR-9."""
import pytest

from budget_store import SqliteBudgetStore


@pytest.fixture()
def budget_store(db_conn):
    return SqliteBudgetStore(conn=db_conn)


MONTH = "2026-05"


# ── upsert_budget ─────────────────────────────────────────────────────────────


def test_upsert_creates_budget_row(budget_store, member_user):
    row = budget_store.upsert_budget(member_user.id, MONTH, expense_budget=3000000)
    assert row["user_id"] == member_user.id
    assert row["month"] == MONTH
    assert row["expense_budget"] == 3000000
    assert row["savings_target"] is None


def test_upsert_sets_savings_target(budget_store, member_user):
    row = budget_store.upsert_budget(member_user.id, MONTH, savings_target=500000)
    assert row["savings_target"] == 500000
    assert row["expense_budget"] is None


def test_upsert_updates_existing_row(budget_store, member_user):
    budget_store.upsert_budget(member_user.id, MONTH, expense_budget=2000000)
    row = budget_store.upsert_budget(member_user.id, MONTH, expense_budget=3000000)
    assert row["expense_budget"] == 3000000


def test_upsert_partial_update_preserves_other_field(budget_store, member_user):
    budget_store.upsert_budget(member_user.id, MONTH, expense_budget=2000000, savings_target=500000)
    row = budget_store.upsert_budget(member_user.id, MONTH, expense_budget=3000000)
    assert row["savings_target"] == 500000


def test_upsert_initialises_alerts_sent_as_empty_json(budget_store, member_user):
    row = budget_store.upsert_budget(member_user.id, MONTH, expense_budget=1000000)
    assert row["alerts_sent"] == "[]"


# ── get_budget ────────────────────────────────────────────────────────────────


def test_get_budget_returns_row(budget_store, member_user):
    budget_store.upsert_budget(member_user.id, MONTH, expense_budget=2000000)
    row = budget_store.get_budget(member_user.id, MONTH)
    assert row is not None
    assert row["expense_budget"] == 2000000


def test_get_budget_returns_none_when_not_set(budget_store, member_user):
    assert budget_store.get_budget(member_user.id, "2026-01") is None


# ── mark_alert_sent / is_alert_sent ───────────────────────────────────────────


def test_alert_not_sent_initially(budget_store, member_user):
    budget_store.upsert_budget(member_user.id, MONTH, expense_budget=1000000)
    assert budget_store.is_alert_sent(member_user.id, MONTH, "80") is False


def test_mark_alert_sent_records_threshold(budget_store, member_user):
    budget_store.upsert_budget(member_user.id, MONTH, expense_budget=1000000)
    budget_store.mark_alert_sent(member_user.id, MONTH, "80")
    assert budget_store.is_alert_sent(member_user.id, MONTH, "80") is True


def test_mark_alert_sent_does_not_affect_other_threshold(budget_store, member_user):
    budget_store.upsert_budget(member_user.id, MONTH, expense_budget=1000000)
    budget_store.mark_alert_sent(member_user.id, MONTH, "80")
    assert budget_store.is_alert_sent(member_user.id, MONTH, "100") is False
