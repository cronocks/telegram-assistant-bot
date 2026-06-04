"""Tests for SqliteCreditCardStore — FR-9 (credit card support)."""
import pytest

from credit_card_store import SqliteCreditCardStore


@pytest.fixture()
def card_store(db_conn):
    return SqliteCreditCardStore(conn=db_conn)


# ── create_card ───────────────────────────────────────────────────────────────


def test_create_card_returns_row(card_store, member_user):
    card = card_store.create_card("Visa ABC", user_id=member_user.id)
    assert card["id"] > 0
    assert card["name"] == "Visa ABC"
    assert card["user_id"] == member_user.id
    assert card["deleted_at"] is None


def test_create_shared_card(card_store):
    card = card_store.create_card("Thẻ chung", user_id=None)
    assert card["user_id"] is None


def test_create_card_rejects_empty_name(card_store, member_user):
    with pytest.raises(ValueError, match="name"):
        card_store.create_card("   ", user_id=member_user.id)


# ── get_card ──────────────────────────────────────────────────────────────────


def test_get_card_returns_row(card_store, member_user):
    created = card_store.create_card("Visa", user_id=member_user.id)
    fetched = card_store.get_card(created["id"])
    assert fetched["id"] == created["id"]


def test_get_card_returns_none_for_missing(card_store):
    assert card_store.get_card(99999) is None


# ── get_card_by_name ──────────────────────────────────────────────────────────


def test_get_card_by_name_is_case_and_diacritic_insensitive(card_store, member_user):
    card_store.create_card("Thẻ Vàng", user_id=member_user.id)
    found = card_store.get_card_by_name(member_user.id, "the vang")
    assert found is not None
    assert found["name"] == "Thẻ Vàng"


def test_get_card_by_name_finds_shared(card_store, member_user):
    card_store.create_card("Chung", user_id=None)
    assert card_store.get_card_by_name(member_user.id, "chung") is not None


def test_get_card_by_name_ignores_other_user(card_store, member_user, another_user):
    card_store.create_card("Riêng", user_id=another_user.id)
    assert card_store.get_card_by_name(member_user.id, "riêng") is None


def test_get_card_by_name_ignores_deleted(card_store, member_user):
    card = card_store.create_card("Cũ", user_id=member_user.id)
    card_store.soft_delete_card(card["id"])
    assert card_store.get_card_by_name(member_user.id, "cũ") is None


# ── list_for_user ─────────────────────────────────────────────────────────────


def test_list_returns_owned_and_shared(card_store, member_user, another_user):
    card_store.create_card("Của tôi", user_id=member_user.id)
    card_store.create_card("Dùng chung", user_id=None)
    card_store.create_card("Của người khác", user_id=another_user.id)

    cards = card_store.list_for_user(member_user.id)
    names = {c["name"] for c in cards}
    assert names == {"Của tôi", "Dùng chung"}


def test_list_excludes_deleted(card_store, member_user):
    card = card_store.create_card("Xóa", user_id=member_user.id)
    card_store.soft_delete_card(card["id"])
    assert card_store.list_for_user(member_user.id) == []


# ── soft_delete_card ──────────────────────────────────────────────────────────


def test_soft_delete_sets_deleted_at(card_store, member_user):
    card = card_store.create_card("Tạm", user_id=member_user.id)
    assert card_store.soft_delete_card(card["id"]) is True
    assert card_store.get_card(card["id"])["deleted_at"] is not None


def test_soft_delete_returns_false_for_missing(card_store):
    assert card_store.soft_delete_card(99999) is False
