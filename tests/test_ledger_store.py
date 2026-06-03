"""Tests for SqliteLedgerStore — FR-9."""
import pytest

from credit_card_store import SqliteCreditCardStore
from ledger_store import SqliteLedgerStore


@pytest.fixture()
def ledger_store(db_conn):
    return SqliteLedgerStore(conn=db_conn)


@pytest.fixture()
def card_store(db_conn):
    return SqliteCreditCardStore(conn=db_conn)


TODAY = "2026-05-26 10:00:00"
YESTERDAY = "2026-05-25 09:00:00"
LAST_MONTH = "2026-04-10 08:00:00"


# ── add_entry ─────────────────────────────────────────────────────────────────


def test_add_expense_entry_returns_row(ledger_store, member_user):
    row = ledger_store.add_entry(
        member_user.id, "expense", 50000, TODAY, note="Ăn trưa"
    )
    assert row["id"] > 0
    assert row["user_id"] == member_user.id
    assert row["kind"] == "expense"
    assert row["amount"] == 50000
    assert row["note"] == "Ăn trưa"
    assert row["source"] == "telegram"
    assert row["voided_at"] is None


def test_add_income_entry(ledger_store, member_user):
    row = ledger_store.add_entry(member_user.id, "income", 5000000, TODAY)
    assert row["kind"] == "income"
    assert row["amount"] == 5000000


def test_add_entry_with_category(ledger_store, member_user, db_conn):
    db_conn.execute(
        "INSERT INTO categories (user_id, name, kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (member_user.id, "Ăn uống", "expense", TODAY, TODAY),
    )
    cat_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = ledger_store.add_entry(
        member_user.id, "expense", 30000, TODAY, category_id=cat_id
    )
    assert row["category_id"] == cat_id


def test_add_entry_rejects_invalid_kind(ledger_store, member_user):
    with pytest.raises(ValueError, match="kind"):
        ledger_store.add_entry(member_user.id, "other", 10000, TODAY)


def test_add_entry_rejects_zero_amount(ledger_store, member_user):
    with pytest.raises(ValueError, match="amount"):
        ledger_store.add_entry(member_user.id, "expense", 0, TODAY)


def test_add_entry_rejects_negative_amount(ledger_store, member_user):
    with pytest.raises(ValueError, match="amount"):
        ledger_store.add_entry(member_user.id, "expense", -100, TODAY)


# ── get_entry ─────────────────────────────────────────────────────────────────


def test_get_entry_returns_row(ledger_store, member_user):
    created = ledger_store.add_entry(member_user.id, "expense", 20000, TODAY)
    fetched = ledger_store.get_entry(created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]


def test_get_entry_returns_none_for_missing(ledger_store):
    assert ledger_store.get_entry(99999) is None


# ── list_for_user ─────────────────────────────────────────────────────────────


def test_list_returns_entries_for_user(ledger_store, member_user, another_user):
    ledger_store.add_entry(member_user.id, "expense", 10000, TODAY)
    ledger_store.add_entry(another_user.id, "expense", 20000, TODAY)

    results = ledger_store.list_for_user(member_user.id)
    assert all(r["user_id"] == member_user.id for r in results)
    assert len(results) == 1


def test_list_excludes_voided(ledger_store, member_user):
    row = ledger_store.add_entry(member_user.id, "expense", 10000, TODAY)
    ledger_store.void_entry(row["id"])
    results = ledger_store.list_for_user(member_user.id)
    assert len(results) == 0


def test_list_filter_by_month(ledger_store, member_user):
    ledger_store.add_entry(member_user.id, "expense", 10000, TODAY)         # May
    ledger_store.add_entry(member_user.id, "expense", 20000, LAST_MONTH)    # April

    may_results = ledger_store.list_for_user(member_user.id, month="2026-05")
    assert all("2026-05" in r["occurred_at"] for r in may_results)
    assert len(may_results) == 1


def test_list_filter_by_kind(ledger_store, member_user):
    ledger_store.add_entry(member_user.id, "expense", 10000, TODAY)
    ledger_store.add_entry(member_user.id, "income", 500000, TODAY)

    expenses = ledger_store.list_for_user(member_user.id, kind="expense")
    assert all(r["kind"] == "expense" for r in expenses)


# ── update_entry ──────────────────────────────────────────────────────────────


def test_update_entry_amount(ledger_store, member_user):
    row = ledger_store.add_entry(member_user.id, "expense", 10000, TODAY)
    updated = ledger_store.update_entry(row["id"], amount=15000)
    assert updated["amount"] == 15000


def test_update_entry_note(ledger_store, member_user):
    row = ledger_store.add_entry(member_user.id, "expense", 10000, TODAY, note="cũ")
    updated = ledger_store.update_entry(row["id"], note="mới")
    assert updated["note"] == "mới"


# ── void_entry ────────────────────────────────────────────────────────────────


def test_void_sets_voided_at(ledger_store, member_user):
    row = ledger_store.add_entry(member_user.id, "expense", 10000, TODAY)
    result = ledger_store.void_entry(row["id"])
    assert result is True
    fetched = ledger_store.get_entry(row["id"])
    assert fetched["voided_at"] is not None


def test_void_returns_false_for_missing(ledger_store):
    assert ledger_store.void_entry(99999) is False


# ── monthly_totals ────────────────────────────────────────────────────────────


def test_monthly_totals_income_and_expense(ledger_store, member_user):
    ledger_store.add_entry(member_user.id, "income", 5000000, TODAY)
    ledger_store.add_entry(member_user.id, "expense", 200000, TODAY)
    ledger_store.add_entry(member_user.id, "expense", 100000, TODAY)

    totals = ledger_store.monthly_totals(member_user.id, "2026-05")
    assert totals["income"] == 5000000
    assert totals["expense"] == 300000


def test_monthly_totals_excludes_voided(ledger_store, member_user):
    row = ledger_store.add_entry(member_user.id, "expense", 50000, TODAY)
    ledger_store.void_entry(row["id"])
    totals = ledger_store.monthly_totals(member_user.id, "2026-05")
    assert totals["expense"] == 0


def test_monthly_totals_zero_when_empty(ledger_store, member_user):
    totals = ledger_store.monthly_totals(member_user.id, "2026-05")
    assert totals["income"] == 0
    assert totals["expense"] == 0


# ── monthly_by_category ───────────────────────────────────────────────────────


def test_monthly_by_category_groups_correctly(ledger_store, member_user, db_conn):
    db_conn.execute(
        "INSERT INTO categories (user_id, name, kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (member_user.id, "Ăn uống", "expense", TODAY, TODAY),
    )
    cat_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    ledger_store.add_entry(member_user.id, "expense", 50000, TODAY, category_id=cat_id)
    ledger_store.add_entry(member_user.id, "expense", 30000, TODAY, category_id=cat_id)
    ledger_store.add_entry(member_user.id, "expense", 20000, TODAY)  # no category

    rows = ledger_store.monthly_by_category(member_user.id, "2026-05")
    cat_row = next((r for r in rows if r["category_id"] == cat_id), None)
    assert cat_row is not None
    assert cat_row["total"] == 80000


# ── credit card support ───────────────────────────────────────────────────────


def test_add_entry_with_credit_card_id(ledger_store, card_store, member_user):
    card = card_store.create_card("Visa", user_id=member_user.id)
    row = ledger_store.add_entry(
        member_user.id, "expense", 50000, TODAY, credit_card_id=card["id"]
    )
    assert row["credit_card_id"] == card["id"]


def test_cc_payment_kind_allowed(ledger_store, card_store, member_user):
    card = card_store.create_card("Visa", user_id=member_user.id)
    row = ledger_store.add_entry(
        member_user.id, "cc_payment", 5000000, TODAY, credit_card_id=card["id"]
    )
    assert row["kind"] == "cc_payment"


def test_monthly_totals_excludes_cc_payment(ledger_store, card_store, member_user):
    """A card purchase counts as expense once; paying the statement must NOT
    add to expense again (this is the whole point of the feature)."""
    card = card_store.create_card("Visa", user_id=member_user.id)
    ledger_store.add_entry(member_user.id, "expense", 100000, TODAY, credit_card_id=card["id"])
    ledger_store.add_entry(member_user.id, "cc_payment", 100000, TODAY, credit_card_id=card["id"])

    totals = ledger_store.monthly_totals(member_user.id, "2026-05")
    assert totals["expense"] == 100000  # not 200000
    assert totals["income"] == 0


def test_card_outstanding(ledger_store, card_store, member_user):
    card = card_store.create_card("Visa", user_id=member_user.id)
    ledger_store.add_entry(member_user.id, "expense", 100000, TODAY, credit_card_id=card["id"])
    ledger_store.add_entry(member_user.id, "expense", 50000, TODAY, credit_card_id=card["id"])
    ledger_store.add_entry(member_user.id, "cc_payment", 80000, TODAY, credit_card_id=card["id"])

    assert ledger_store.card_outstanding(member_user.id, card["id"]) == 70000


def test_card_outstanding_excludes_voided(ledger_store, card_store, member_user):
    card = card_store.create_card("Visa", user_id=member_user.id)
    e = ledger_store.add_entry(member_user.id, "expense", 100000, TODAY, credit_card_id=card["id"])
    ledger_store.add_entry(member_user.id, "expense", 30000, TODAY, credit_card_id=card["id"])
    ledger_store.void_entry(e["id"])

    assert ledger_store.card_outstanding(member_user.id, card["id"]) == 30000


def test_all_card_outstanding_maps_per_card(ledger_store, card_store, member_user):
    visa = card_store.create_card("Visa", user_id=member_user.id)
    master = card_store.create_card("Master", user_id=member_user.id)
    ledger_store.add_entry(member_user.id, "expense", 100000, TODAY, credit_card_id=visa["id"])
    ledger_store.add_entry(member_user.id, "cc_payment", 40000, TODAY, credit_card_id=visa["id"])
    ledger_store.add_entry(member_user.id, "expense", 20000, TODAY, credit_card_id=master["id"])

    result = ledger_store.all_card_outstanding(member_user.id)
    assert result[visa["id"]] == 60000
    assert result[master["id"]] == 20000
