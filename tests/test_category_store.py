"""Tests for SqliteCategoryStore — FR-9."""
import pytest

from category_store import SqliteCategoryStore


@pytest.fixture()
def cat_store(db_conn):
    return SqliteCategoryStore(conn=db_conn)


# ── create_category ───────────────────────────────────────────────────────────


def test_create_personal_expense_category(cat_store, member_user):
    row = cat_store.create_category("Ăn uống", "expense", user_id=member_user.id)
    assert row["id"] > 0
    assert row["user_id"] == member_user.id
    assert row["name"] == "Ăn uống"
    assert row["kind"] == "expense"
    assert row["parent_id"] is None
    assert row["deleted_at"] is None


def test_create_personal_income_category(cat_store, member_user):
    row = cat_store.create_category("Lương", "income", user_id=member_user.id)
    assert row["kind"] == "income"
    assert row["user_id"] == member_user.id


def test_create_family_shared_category(cat_store):
    row = cat_store.create_category("Tiền nhà", "expense", user_id=None)
    assert row["user_id"] is None
    assert row["name"] == "Tiền nhà"


def test_create_rejects_empty_name(cat_store, member_user):
    with pytest.raises(ValueError, match="name"):
        cat_store.create_category("", "expense", user_id=member_user.id)


def test_create_rejects_whitespace_name(cat_store, member_user):
    with pytest.raises(ValueError, match="name"):
        cat_store.create_category("   ", "expense", user_id=member_user.id)


def test_create_rejects_invalid_kind(cat_store, member_user):
    with pytest.raises(ValueError, match="kind"):
        cat_store.create_category("Misc", "other", user_id=member_user.id)


# ── get_category ──────────────────────────────────────────────────────────────


def test_get_category_returns_row(cat_store, member_user):
    created = cat_store.create_category("Di chuyển", "expense", user_id=member_user.id)
    fetched = cat_store.get_category(created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["name"] == "Di chuyển"


def test_get_category_returns_none_for_missing(cat_store):
    assert cat_store.get_category(99999) is None


# ── list_for_user ─────────────────────────────────────────────────────────────


def test_list_includes_personal_and_shared(cat_store, member_user, another_user):
    cat_store.create_category("Ăn uống", "expense", user_id=member_user.id)
    cat_store.create_category("Tiền điện", "expense", user_id=None)  # shared
    cat_store.create_category("Đi chợ", "expense", user_id=another_user.id)  # other user

    results = cat_store.list_for_user(member_user.id)
    names = {r["name"] for r in results}
    assert "Ăn uống" in names
    assert "Tiền điện" in names
    assert "Đi chợ" not in names


def test_list_excludes_deleted(cat_store, member_user):
    row = cat_store.create_category("Cũ", "expense", user_id=member_user.id)
    cat_store.soft_delete_category(row["id"])

    results = cat_store.list_for_user(member_user.id)
    assert all(r["name"] != "Cũ" for r in results)


def test_list_filter_by_kind(cat_store, member_user):
    cat_store.create_category("Ăn uống", "expense", user_id=member_user.id)
    cat_store.create_category("Lương", "income", user_id=member_user.id)

    expense_only = cat_store.list_for_user(member_user.id, kind="expense")
    assert all(r["kind"] == "expense" for r in expense_only)
    assert any(r["name"] == "Ăn uống" for r in expense_only)


# ── update_category ───────────────────────────────────────────────────────────


def test_update_category_name(cat_store, member_user):
    row = cat_store.create_category("Café", "expense", user_id=member_user.id)
    updated = cat_store.update_category(row["id"], name="Cà phê")
    assert updated["name"] == "Cà phê"


# ── soft_delete_category ──────────────────────────────────────────────────────


def test_soft_delete_sets_deleted_at(cat_store, member_user):
    row = cat_store.create_category("Xóa đi", "expense", user_id=member_user.id)
    result = cat_store.soft_delete_category(row["id"])
    assert result is True
    fetched = cat_store.get_category(row["id"])
    assert fetched["deleted_at"] is not None


def test_soft_delete_returns_false_for_missing(cat_store):
    assert cat_store.soft_delete_category(99999) is False
