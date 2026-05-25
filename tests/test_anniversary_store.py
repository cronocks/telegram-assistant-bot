"""Tests for SqliteAnniversaryStore — FR-8."""
import pytest

from anniversary_store import SqliteAnniversaryStore


@pytest.fixture()
def anniv_store(db_conn):
    return SqliteAnniversaryStore(conn=db_conn)


# ── create_anniversary ────────────────────────────────────────────────────────


def test_create_returns_full_row(anniv_store, member_user):
    row = anniv_store.create_anniversary(
        user_id=member_user.id,
        name="Giỗ ông nội",
        date_type="lunar",
        month=3,
        day=10,
        category="gio",
    )
    assert row is not None
    assert row["id"] > 0
    assert row["user_id"] == member_user.id
    assert row["name"] == "Giỗ ông nội"
    assert row["date_type"] == "lunar"
    assert row["month"] == 3
    assert row["day"] == 10
    assert row["category"] == "gio"
    assert row["enabled"] == 1
    assert row["reminder_offsets"] == "30,15,7,3,1,0"
    assert row["deleted_at"] is None


def test_create_with_custom_offsets(anniv_store, member_user):
    row = anniv_store.create_anniversary(
        user_id=member_user.id,
        name="Kỷ niệm cưới",
        date_type="solar",
        month=8,
        day=15,
        category="cuoi",
        reminder_offsets="7,3,1,0",
        note="Lần đầu gặp ở quán cafe",
    )
    assert row["reminder_offsets"] == "7,3,1,0"
    assert row["note"] == "Lần đầu gặp ở quán cafe"


def test_create_rejects_empty_name(anniv_store, member_user):
    with pytest.raises(ValueError):
        anniv_store.create_anniversary(
            user_id=member_user.id,
            name="",
            date_type="lunar",
            month=3,
            day=10,
        )


def test_create_rejects_invalid_date_type(anniv_store, member_user):
    with pytest.raises(ValueError):
        anniv_store.create_anniversary(
            user_id=member_user.id,
            name="Test",
            date_type="hebrew",
            month=3,
            day=10,
        )


def test_create_rejects_out_of_range_month(anniv_store, member_user):
    with pytest.raises(ValueError):
        anniv_store.create_anniversary(
            user_id=member_user.id,
            name="Test",
            date_type="lunar",
            month=13,
            day=10,
        )


def test_create_rejects_out_of_range_day(anniv_store, member_user):
    with pytest.raises(ValueError):
        anniv_store.create_anniversary(
            user_id=member_user.id,
            name="Test",
            date_type="solar",
            month=2,
            day=31,
        )


# ── get_anniversary ───────────────────────────────────────────────────────────


def test_get_returns_row(anniv_store, member_user):
    created = anniv_store.create_anniversary(
        user_id=member_user.id, name="X", date_type="lunar", month=1, day=1,
    )
    fetched = anniv_store.get_anniversary(created["id"])
    assert fetched["id"] == created["id"]
    assert fetched["name"] == "X"


def test_get_returns_none_for_missing(anniv_store):
    assert anniv_store.get_anniversary(99999) is None


# ── list_for_user ─────────────────────────────────────────────────────────────


def test_list_for_user_returns_only_user_rows(anniv_store, member_user, another_user):
    anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    anniv_store.create_anniversary(
        user_id=another_user.id, name="B", date_type="lunar", month=2, day=2,
    )
    rows = anniv_store.list_for_user(member_user.id)
    assert len(rows) == 1
    assert rows[0]["name"] == "A"


def test_list_excludes_soft_deleted(anniv_store, member_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    anniv_store.create_anniversary(
        user_id=member_user.id, name="B", date_type="lunar", month=2, day=2,
    )
    anniv_store.soft_delete_anniversary(a["id"])
    rows = anniv_store.list_for_user(member_user.id)
    assert len(rows) == 1
    assert rows[0]["name"] == "B"


def test_list_includes_deleted_when_flag(anniv_store, member_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    anniv_store.soft_delete_anniversary(a["id"])
    rows = anniv_store.list_for_user(member_user.id, include_deleted=True)
    assert len(rows) == 1


# ── list_all_active ───────────────────────────────────────────────────────────


def test_list_all_active_returns_enabled_only(anniv_store, member_user, another_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    b = anniv_store.create_anniversary(
        user_id=another_user.id, name="B", date_type="solar", month=6, day=6,
    )
    c = anniv_store.create_anniversary(
        user_id=member_user.id, name="C-paused", date_type="lunar", month=3, day=3,
    )
    anniv_store.update_anniversary(c["id"], enabled=0)
    rows = anniv_store.list_all_active()
    names = {r["name"] for r in rows}
    assert names == {"A", "B"}


def test_list_all_active_excludes_deleted(anniv_store, member_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    anniv_store.soft_delete_anniversary(a["id"])
    assert anniv_store.list_all_active() == []


# ── update_anniversary ────────────────────────────────────────────────────────


def test_update_changes_fields(anniv_store, member_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    updated = anniv_store.update_anniversary(
        a["id"], name="A renamed", month=5, day=20, reminder_offsets="7,3,0",
    )
    assert updated["name"] == "A renamed"
    assert updated["month"] == 5
    assert updated["day"] == 20
    assert updated["reminder_offsets"] == "7,3,0"


def test_update_ignores_unknown_fields(anniv_store, member_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    updated = anniv_store.update_anniversary(a["id"], hacker_field="boom")
    assert updated is not None
    assert "hacker_field" not in updated


def test_update_returns_none_for_missing(anniv_store):
    assert anniv_store.update_anniversary(99999, name="ghost") is None


# ── soft_delete / restore ─────────────────────────────────────────────────────


def test_soft_delete_marks_deleted_at(anniv_store, member_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    assert anniv_store.soft_delete_anniversary(a["id"]) is True
    row = anniv_store.get_anniversary(a["id"])
    assert row["deleted_at"] is not None


def test_soft_delete_returns_false_for_missing(anniv_store):
    assert anniv_store.soft_delete_anniversary(99999) is False


def test_soft_delete_returns_false_when_already_deleted(anniv_store, member_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    anniv_store.soft_delete_anniversary(a["id"])
    assert anniv_store.soft_delete_anniversary(a["id"]) is False


def test_restore_clears_deleted_at(anniv_store, member_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="lunar", month=1, day=1,
    )
    anniv_store.soft_delete_anniversary(a["id"])
    assert anniv_store.restore_anniversary(a["id"]) is True
    row = anniv_store.get_anniversary(a["id"])
    assert row["deleted_at"] is None


# ── is_leap_month ─────────────────────────────────────────────────────────────


def test_create_with_is_leap_month_true(anniv_store, member_user):
    row = anniv_store.create_anniversary(
        user_id=member_user.id, name="Giỗ tháng nhuận", date_type="lunar",
        month=6, day=9, is_leap_month=1,
    )
    assert row["is_leap_month"] == 1


def test_create_default_is_leap_month_zero(anniv_store, member_user):
    row = anniv_store.create_anniversary(
        user_id=member_user.id, name="Giỗ thường", date_type="lunar",
        month=3, day=10,
    )
    assert row["is_leap_month"] == 0


def test_update_is_leap_month(anniv_store, member_user):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="X", date_type="lunar", month=6, day=9,
    )
    assert a["is_leap_month"] == 0
    updated = anniv_store.update_anniversary(a["id"], is_leap_month=1)
    assert updated["is_leap_month"] == 1
