"""Tests for SqliteFamilyStore — FR-11 Phase A."""
import pytest

from family_store import SqliteFamilyStore


@pytest.fixture()
def family_store(db_conn):
    return SqliteFamilyStore(conn=db_conn)


# ── create_member / get_member ────────────────────────────────────────────────


def test_create_returns_full_row(family_store, sample_admin):
    row = family_store.create_member(
        created_by=sample_admin.id,
        full_name="Nguyễn Văn A",
        generation=3,
        gender="nam",
        birth_date_type="lunar",
        birth_year=1920,
        birth_month=2,
        birth_day=10,
        death_date_type="lunar",
        death_year=1990,
        death_month=7,
        death_day=15,
    )
    assert row is not None
    assert row["id"] > 0
    assert row["full_name"] == "Nguyễn Văn A"
    assert row["generation"] == 3
    assert row["gender"] == "nam"
    assert row["birth_date_type"] == "lunar"
    assert row["birth_year"] == 1920
    assert row["death_day"] == 15
    assert row["birth_approx"] == 0
    assert row["created_by"] == sample_admin.id
    assert row["deleted_at"] is None


def test_create_minimal_name_only(family_store, sample_admin):
    row = family_store.create_member(created_by=sample_admin.id, full_name="Cụ Tổ")
    assert row["full_name"] == "Cụ Tổ"
    assert row["generation"] is None
    assert row["birth_year"] is None
    assert row["death_year"] is None


def test_create_year_only_approx(family_store, sample_admin):
    row = family_store.create_member(
        created_by=sample_admin.id,
        full_name="Cụ B",
        birth_year=1880,
        birth_approx=1,
    )
    assert row["birth_year"] == 1880
    assert row["birth_approx"] == 1
    assert row["birth_month"] is None


def test_create_rejects_empty_name(family_store, sample_admin):
    with pytest.raises(ValueError):
        family_store.create_member(created_by=sample_admin.id, full_name="  ")


def test_create_rejects_invalid_gender(family_store, sample_admin):
    with pytest.raises(ValueError):
        family_store.create_member(
            created_by=sample_admin.id, full_name="X", gender="other",
        )


def test_create_rejects_invalid_date_type(family_store, sample_admin):
    with pytest.raises(ValueError):
        family_store.create_member(
            created_by=sample_admin.id, full_name="X", birth_date_type="hebrew",
        )


def test_create_rejects_month_without_year(family_store, sample_admin):
    with pytest.raises(ValueError):
        family_store.create_member(
            created_by=sample_admin.id, full_name="X", birth_month=3,
        )


def test_create_rejects_out_of_range_month(family_store, sample_admin):
    with pytest.raises(ValueError):
        family_store.create_member(
            created_by=sample_admin.id, full_name="X",
            birth_year=1950, birth_month=13, birth_day=1,
        )


def test_create_rejects_day_without_month(family_store, sample_admin):
    with pytest.raises(ValueError):
        family_store.create_member(
            created_by=sample_admin.id, full_name="X",
            birth_year=1950, birth_day=5,
        )


def test_get_missing_returns_none(family_store):
    assert family_store.get_member(9999) is None


# ── list_members / search_by_name ─────────────────────────────────────────────


def test_list_orders_by_generation_then_name(family_store, sample_admin):
    family_store.create_member(created_by=sample_admin.id, full_name="Nguyễn Văn B", generation=2)
    family_store.create_member(created_by=sample_admin.id, full_name="Nguyễn Văn A", generation=2)
    family_store.create_member(created_by=sample_admin.id, full_name="Cụ Tổ", generation=1)
    rows = family_store.list_members()
    names = [r["full_name"] for r in rows]
    assert names == ["Cụ Tổ", "Nguyễn Văn A", "Nguyễn Văn B"]


def test_list_filters_by_generation(family_store, sample_admin):
    family_store.create_member(created_by=sample_admin.id, full_name="A", generation=1)
    family_store.create_member(created_by=sample_admin.id, full_name="B", generation=2)
    rows = family_store.list_members(generation=2)
    assert len(rows) == 1
    assert rows[0]["full_name"] == "B"


def test_list_excludes_deleted(family_store, sample_admin):
    row = family_store.create_member(created_by=sample_admin.id, full_name="X")
    family_store.soft_delete_member(row["id"])
    assert family_store.list_members() == []


def test_search_matches_diacritic_insensitive_substring(family_store, sample_admin):
    family_store.create_member(created_by=sample_admin.id, full_name="Nguyễn Văn Ất")
    rows = family_store.search_by_name("van at")
    assert len(rows) == 1
    assert rows[0]["full_name"] == "Nguyễn Văn Ất"


def test_search_matches_alias_name(family_store, sample_admin):
    family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn A", alias_name="Ông Nội",
    )
    rows = family_store.search_by_name("ong noi")
    assert len(rows) == 1


def test_search_no_match_returns_empty(family_store, sample_admin):
    family_store.create_member(created_by=sample_admin.id, full_name="A")
    assert family_store.search_by_name("khong ton tai") == []


def test_search_excludes_deleted(family_store, sample_admin):
    row = family_store.create_member(created_by=sample_admin.id, full_name="Nguyễn Văn A")
    family_store.soft_delete_member(row["id"])
    assert family_store.search_by_name("van a") == []


# ── update_member ─────────────────────────────────────────────────────────────


def test_update_changes_fields(family_store, sample_admin):
    row = family_store.create_member(created_by=sample_admin.id, full_name="A", generation=2)
    updated = family_store.update_member(row["id"], generation=3, bio="Trưởng chi 2")
    assert updated["generation"] == 3
    assert updated["bio"] == "Trưởng chi 2"
    assert updated["updated_at"] >= row["updated_at"]


def test_update_missing_returns_none(family_store):
    assert family_store.update_member(9999, generation=1) is None


def test_update_rejects_invalid_date_combo(family_store, sample_admin):
    row = family_store.create_member(created_by=sample_admin.id, full_name="A")
    with pytest.raises(ValueError):
        family_store.update_member(row["id"], birth_month=5)  # month without year


def test_update_rejects_empty_name(family_store, sample_admin):
    row = family_store.create_member(created_by=sample_admin.id, full_name="A")
    with pytest.raises(ValueError):
        family_store.update_member(row["id"], full_name=" ")


def test_update_validates_against_existing_date_fields(family_store, sample_admin):
    row = family_store.create_member(
        created_by=sample_admin.id, full_name="A",
        birth_year=1950, birth_month=3, birth_day=10,
    )
    # Changing only the day must still validate against stored month/year.
    updated = family_store.update_member(row["id"], birth_day=20)
    assert updated["birth_day"] == 20
    with pytest.raises(ValueError):
        family_store.update_member(row["id"], birth_day=40)


# ── soft delete / restore ─────────────────────────────────────────────────────


def test_soft_delete_then_restore(family_store, sample_admin):
    row = family_store.create_member(created_by=sample_admin.id, full_name="A")
    assert family_store.soft_delete_member(row["id"]) is True
    assert family_store.get_member(row["id"])["deleted_at"] is not None
    assert family_store.restore_member(row["id"]) is True
    assert family_store.get_member(row["id"])["deleted_at"] is None


def test_soft_delete_twice_returns_false(family_store, sample_admin):
    row = family_store.create_member(created_by=sample_admin.id, full_name="A")
    family_store.soft_delete_member(row["id"])
    assert family_store.soft_delete_member(row["id"]) is False
