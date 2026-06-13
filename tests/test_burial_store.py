"""Tests for SqliteBurialStore — FR-11 Phase A."""
import pytest

from burial_store import SqliteBurialStore
from family_store import SqliteFamilyStore


@pytest.fixture()
def family_store(db_conn):
    return SqliteFamilyStore(conn=db_conn)


@pytest.fixture()
def burial_store(db_conn):
    return SqliteBurialStore(conn=db_conn)


@pytest.fixture()
def member(family_store, sample_admin):
    return family_store.create_member(created_by=sample_admin.id, full_name="Nguyễn Văn A")


# ── create_record / get_record ────────────────────────────────────────────────


def test_create_returns_full_row(burial_store, member, sample_admin):
    row = burial_store.create_record(
        created_by=sample_admin.id,
        member_id=member["id"],
        cemetery_name="Nghĩa trang Văn Điển",
        address="Thanh Trì, Hà Nội",
        lat=20.9456,
        lng=105.8231,
        plot_info="Lô B3, hàng 12",
        note="Cạnh mộ cụ X, đi từ cổng phụ",
    )
    assert row["id"] > 0
    assert row["member_id"] == member["id"]
    assert row["cemetery_name"] == "Nghĩa trang Văn Điển"
    assert row["lat"] == pytest.approx(20.9456)
    assert row["lng"] == pytest.approx(105.8231)
    assert row["is_current"] == 1
    assert row["deleted_at"] is None


def test_create_rejects_empty_cemetery(burial_store, member, sample_admin):
    with pytest.raises(ValueError):
        burial_store.create_record(
            created_by=sample_admin.id, member_id=member["id"], cemetery_name="",
        )


def test_create_rejects_lat_out_of_range(burial_store, member, sample_admin):
    with pytest.raises(ValueError):
        burial_store.create_record(
            created_by=sample_admin.id, member_id=member["id"],
            cemetery_name="X", lat=91.0, lng=105.0,
        )


def test_create_rejects_lng_out_of_range(burial_store, member, sample_admin):
    with pytest.raises(ValueError):
        burial_store.create_record(
            created_by=sample_admin.id, member_id=member["id"],
            cemetery_name="X", lat=20.0, lng=181.0,
        )


def test_create_rejects_partial_gps(burial_store, member, sample_admin):
    with pytest.raises(ValueError):
        burial_store.create_record(
            created_by=sample_admin.id, member_id=member["id"],
            cemetery_name="X", lat=20.0,
        )


def test_create_rejects_missing_member(burial_store, sample_admin):
    with pytest.raises(ValueError):
        burial_store.create_record(
            created_by=sample_admin.id, member_id=9999, cemetery_name="X",
        )


def test_new_record_demotes_previous_current(burial_store, member, sample_admin):
    first = burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="Chỗ cũ",
    )
    second = burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="Chỗ mới",
    )
    assert second["is_current"] == 1
    assert burial_store.get_record(first["id"])["is_current"] == 0


def test_get_missing_returns_none(burial_store):
    assert burial_store.get_record(9999) is None


# ── current / list for member ─────────────────────────────────────────────────


def test_get_current_for_member(burial_store, member, sample_admin):
    burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="Chỗ cũ",
    )
    burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="Chỗ mới",
    )
    current = burial_store.get_current_for_member(member["id"])
    assert current["cemetery_name"] == "Chỗ mới"


def test_get_current_none_when_no_record(burial_store, member):
    assert burial_store.get_current_for_member(member["id"]) is None


def test_list_for_member_newest_first(burial_store, member, sample_admin):
    burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="Chỗ cũ",
    )
    burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="Chỗ mới",
    )
    rows = burial_store.list_for_member(member["id"])
    assert [r["cemetery_name"] for r in rows] == ["Chỗ mới", "Chỗ cũ"]


def test_list_excludes_deleted(burial_store, member, sample_admin):
    row = burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="X",
    )
    burial_store.soft_delete_record(row["id"])
    assert burial_store.list_for_member(member["id"]) == []


# ── update_record ─────────────────────────────────────────────────────────────


def test_update_changes_fields(burial_store, member, sample_admin):
    row = burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="X",
    )
    updated = burial_store.update_record(
        row["id"], address="Địa chỉ mới", lat=21.0, lng=105.5,
    )
    assert updated["address"] == "Địa chỉ mới"
    assert updated["lat"] == pytest.approx(21.0)


def test_update_missing_returns_none(burial_store):
    assert burial_store.update_record(9999, note="x") is None


def test_update_rejects_invalid_gps(burial_store, member, sample_admin):
    row = burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="X",
    )
    with pytest.raises(ValueError):
        burial_store.update_record(row["id"], lat=99.0, lng=105.0)


def test_update_gps_against_existing_pair(burial_store, member, sample_admin):
    row = burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"],
        cemetery_name="X", lat=20.0, lng=105.0,
    )
    # Updating only lat keeps the stored lng — must remain a valid pair.
    updated = burial_store.update_record(row["id"], lat=21.0)
    assert updated["lat"] == pytest.approx(21.0)
    assert updated["lng"] == pytest.approx(105.0)


def test_update_rejects_empty_cemetery(burial_store, member, sample_admin):
    row = burial_store.create_record(
        created_by=sample_admin.id, member_id=member["id"], cemetery_name="X",
    )
    with pytest.raises(ValueError):
        burial_store.update_record(row["id"], cemetery_name=" ")
