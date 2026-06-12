"""Tests for FR-11 cmd_family parsers and handlers."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from burial_store import SqliteBurialStore
from cmd_family import (
    ParseFamilyError,
    _cmd_danh_sach_nguoi_than,
    _cmd_sua_nguoi_than,
    _cmd_them_mo_phan,
    _cmd_them_nguoi_than,
    _cmd_tim_mo,
    _cmd_xem_nguoi_than,
    _cmd_xoa_mo_phan,
    _cmd_xoa_nguoi_than,
    parse_burial_input,
    parse_edit_pairs,
    parse_member_input,
)
from deps import CoreDeps
from family_store import SqliteFamilyStore


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text, use_markdown=True):
        self.sent.append((chat_id, text))

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


@pytest.fixture()
def family_store(db_conn):
    return SqliteFamilyStore(conn=db_conn)


@pytest.fixture()
def burial_store(db_conn):
    return SqliteBurialStore(conn=db_conn)


@pytest.fixture()
def fake_channel():
    return FakeChannel()


@pytest.fixture()
def deps(family_store, burial_store, fake_channel):
    d = MagicMock(spec=CoreDeps)
    d.channel = fake_channel
    d.family_store = family_store
    d.burial_store = burial_store
    d.audit = MagicMock()
    return d


def _run(coro):
    return asyncio.run(coro)


# ── parse_member_input ────────────────────────────────────────────────────────


def test_parse_member_name_only():
    p = parse_member_input("Nguyễn Văn A")
    assert p == {"full_name": "Nguyễn Văn A"}


def test_parse_member_full():
    p = parse_member_input(
        "Nguyễn Văn A, doi 3, sinh am 10/2/1920, mat am 15/7/1990, gioi tinh nam, ghi chu Trưởng chi"
    )
    assert p["full_name"] == "Nguyễn Văn A"
    assert p["generation"] == 3
    assert p["birth_date_type"] == "lunar"
    assert p["birth_day"] == 10
    assert p["birth_month"] == 2
    assert p["birth_year"] == 1920
    assert p["death_date_type"] == "lunar"
    assert p["death_day"] == 15
    assert p["death_month"] == 7
    assert p["death_year"] == 1990
    assert p["gender"] == "nam"
    assert p["bio"] == "Trưởng chi"


def test_parse_member_with_diacritics_keywords():
    p = parse_member_input("Bà B, đời 2, giới tính nữ, sinh dương 1/1/1940")
    assert p["generation"] == 2
    assert p["gender"] == "nu"
    assert p["birth_date_type"] == "solar"


def test_parse_member_year_only():
    p = parse_member_input("Cụ C, sinh 1880")
    assert p["birth_year"] == 1880
    assert "birth_month" not in p
    assert "birth_date_type" not in p


def test_parse_member_approx_year():
    p = parse_member_input("Cụ D, mat khoang 1900")
    assert p["death_year"] == 1900
    assert p["death_approx"] == 1


def test_parse_member_alias():
    p = parse_member_input("Nguyễn Văn A, ten goi Ông Nội")
    assert p["alias_name"] == "Ông Nội"


def test_parse_member_rejects_empty_name():
    with pytest.raises(ParseFamilyError):
        parse_member_input(" , doi 3")


def test_parse_member_rejects_bad_date():
    with pytest.raises(ParseFamilyError):
        parse_member_input("X, sinh am 10/13/1920")


def test_parse_member_rejects_unknown_segment():
    with pytest.raises(ParseFamilyError):
        parse_member_input("X, tuoi 99")


def test_parse_member_rejects_bad_gender():
    with pytest.raises(ParseFamilyError):
        parse_member_input("X, gioi tinh khac")


# ── parse_burial_input ────────────────────────────────────────────────────────


def test_parse_burial_full():
    member_id, cemetery, fields = parse_burial_input(
        "5, Nghĩa trang Văn Điển, dia chi Thanh Trì Hà Nội, "
        "gps 20.9456,105.8231, lo B3 hàng 12, ghi chu Cạnh mộ cụ X"
    )
    assert member_id == 5
    assert cemetery == "Nghĩa trang Văn Điển"
    assert fields["address"] == "Thanh Trì Hà Nội"
    assert fields["lat"] == pytest.approx(20.9456)
    assert fields["lng"] == pytest.approx(105.8231)
    assert fields["plot_info"] == "B3 hàng 12"
    assert fields["note"] == "Cạnh mộ cụ X"


def test_parse_burial_minimal():
    member_id, cemetery, fields = parse_burial_input("7, Nghĩa trang quê")
    assert member_id == 7
    assert cemetery == "Nghĩa trang quê"
    assert fields == {}


def test_parse_burial_rejects_missing_id():
    with pytest.raises(ParseFamilyError):
        parse_burial_input("Nghĩa trang Văn Điển")


def test_parse_burial_rejects_missing_cemetery():
    with pytest.raises(ParseFamilyError):
        parse_burial_input("5")


def test_parse_burial_rejects_bad_gps():
    with pytest.raises(ParseFamilyError):
        parse_burial_input("5, X, gps abc,def")


# ── parse_edit_pairs ──────────────────────────────────────────────────────────


def test_parse_edit_pairs_member():
    target_id, updates = parse_edit_pairs("5, doi=4, ten=Nguyễn Văn B")
    assert target_id == 5
    assert updates["generation"] == 4
    assert updates["full_name"] == "Nguyễn Văn B"


def test_parse_edit_pairs_dates():
    _, updates = parse_edit_pairs("5, sinh=am 10/2/1920")
    assert updates["birth_date_type"] == "lunar"
    assert updates["birth_year"] == 1920


def test_parse_edit_pairs_rejects_unknown_field():
    with pytest.raises(ParseFamilyError):
        parse_edit_pairs("5, mau=do")


def test_parse_edit_pairs_rejects_missing_id():
    with pytest.raises(ParseFamilyError):
        parse_edit_pairs("doi=4")


# ── _cmd_them_nguoi_than ──────────────────────────────────────────────────────


def test_them_nguoi_than_denied_for_member(deps, fake_channel, member_user):
    _run(_cmd_them_nguoi_than("c1", "Nguyễn Văn A", member_user, deps))
    assert "không có quyền" in fake_channel.last_text.lower()
    assert deps.family_store.list_members() == []


def test_them_nguoi_than_creates_row(deps, fake_channel, sample_admin):
    _run(_cmd_them_nguoi_than(
        "c1", "Nguyễn Văn A, doi 3, mat am 15/7/1990", sample_admin, deps,
    ))
    rows = deps.family_store.list_members()
    assert len(rows) == 1
    assert rows[0]["generation"] == 3
    assert f"#{rows[0]['id']}" in fake_channel.last_text
    deps.audit.log.assert_called_once()


def test_them_nguoi_than_empty_body_shows_usage(deps, fake_channel, sample_admin):
    _run(_cmd_them_nguoi_than("c1", "  ", sample_admin, deps))
    assert "them nguoi than" in fake_channel.last_text


def test_them_nguoi_than_parse_error(deps, fake_channel, sample_admin):
    _run(_cmd_them_nguoi_than("c1", "X, tuoi 99", sample_admin, deps))
    assert "⚠️" in fake_channel.last_text


def test_them_nguoi_than_not_enabled(fake_channel, sample_admin):
    d = MagicMock(spec=CoreDeps)
    d.channel = fake_channel
    d.family_store = None
    d.burial_store = None
    _run(_cmd_them_nguoi_than("c1", "X", sample_admin, d))
    assert "chưa được kích hoạt" in fake_channel.last_text


# ── _cmd_danh_sach_nguoi_than ─────────────────────────────────────────────────


def test_danh_sach_empty(deps, fake_channel, member_user):
    _run(_cmd_danh_sach_nguoi_than("c1", "", member_user, deps))
    assert "chưa có" in fake_channel.last_text.lower()


def test_danh_sach_lists_members(deps, fake_channel, sample_admin, member_user):
    deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Cụ Tổ", generation=1,
    )
    deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn A", generation=2,
    )
    _run(_cmd_danh_sach_nguoi_than("c1", "", member_user, deps))
    assert "Cụ Tổ" in fake_channel.last_text
    assert "Nguyễn Văn A" in fake_channel.last_text


def test_danh_sach_filters_by_generation(deps, fake_channel, sample_admin, member_user):
    deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Cụ Tổ", generation=1,
    )
    deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn A", generation=2,
    )
    _run(_cmd_danh_sach_nguoi_than("c1", "doi 2", member_user, deps))
    assert "Cụ Tổ" not in fake_channel.last_text
    assert "Nguyễn Văn A" in fake_channel.last_text


# ── _cmd_xem_nguoi_than ───────────────────────────────────────────────────────


def test_xem_by_id_includes_burial_and_maps(deps, fake_channel, sample_admin, member_user):
    row = deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn A",
        death_date_type="lunar", death_year=1990, death_month=7, death_day=15,
    )
    deps.burial_store.create_record(
        created_by=sample_admin.id, member_id=row["id"],
        cemetery_name="Nghĩa trang Văn Điển", lat=20.9456, lng=105.8231,
    )
    _run(_cmd_xem_nguoi_than("c1", str(row["id"]), member_user, deps))
    text = fake_channel.last_text
    assert "Nguyễn Văn A" in text
    assert "Nghĩa trang Văn Điển" in text
    assert "https://maps.google.com/?q=20.9456,105.8231" in text


def test_xem_by_name(deps, fake_channel, sample_admin, member_user):
    deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn Ất",
    )
    _run(_cmd_xem_nguoi_than("c1", "van at", member_user, deps))
    assert "Nguyễn Văn Ất" in fake_channel.last_text


def test_xem_missing(deps, fake_channel, member_user):
    _run(_cmd_xem_nguoi_than("c1", "9999", member_user, deps))
    assert "không tìm thấy" in fake_channel.last_text.lower()


# ── _cmd_sua_nguoi_than / _cmd_xoa_nguoi_than ─────────────────────────────────


def test_sua_nguoi_than_updates(deps, fake_channel, sample_admin):
    row = deps.family_store.create_member(
        created_by=sample_admin.id, full_name="A", generation=2,
    )
    _run(_cmd_sua_nguoi_than("c1", f"{row['id']}, doi=3", sample_admin, deps))
    assert deps.family_store.get_member(row["id"])["generation"] == 3
    deps.audit.log.assert_called_once()


def test_sua_nguoi_than_denied_for_member(deps, fake_channel, sample_admin, member_user):
    row = deps.family_store.create_member(
        created_by=sample_admin.id, full_name="A", generation=2,
    )
    _run(_cmd_sua_nguoi_than("c1", f"{row['id']}, doi=3", member_user, deps))
    assert "không có quyền" in fake_channel.last_text.lower()
    assert deps.family_store.get_member(row["id"])["generation"] == 2


def test_xoa_nguoi_than_blocked_by_burial(deps, fake_channel, sample_admin):
    row = deps.family_store.create_member(created_by=sample_admin.id, full_name="A")
    deps.burial_store.create_record(
        created_by=sample_admin.id, member_id=row["id"], cemetery_name="X",
    )
    _run(_cmd_xoa_nguoi_than("c1", str(row["id"]), sample_admin, deps))
    assert "mộ phần" in fake_channel.last_text.lower()
    assert deps.family_store.get_member(row["id"])["deleted_at"] is None


def test_xoa_nguoi_than_success(deps, fake_channel, sample_admin):
    row = deps.family_store.create_member(created_by=sample_admin.id, full_name="A")
    _run(_cmd_xoa_nguoi_than("c1", str(row["id"]), sample_admin, deps))
    assert deps.family_store.get_member(row["id"])["deleted_at"] is not None
    deps.audit.log.assert_called_once()


# ── _cmd_them_mo_phan / _cmd_xoa_mo_phan ──────────────────────────────────────


def test_them_mo_phan_creates(deps, fake_channel, sample_admin):
    row = deps.family_store.create_member(created_by=sample_admin.id, full_name="A")
    _run(_cmd_them_mo_phan(
        "c1", f"{row['id']}, Nghĩa trang Văn Điển, gps 20.9,105.8", sample_admin, deps,
    ))
    current = deps.burial_store.get_current_for_member(row["id"])
    assert current is not None
    assert current["cemetery_name"] == "Nghĩa trang Văn Điển"
    deps.audit.log.assert_called_once()


def test_them_mo_phan_missing_member(deps, fake_channel, sample_admin):
    _run(_cmd_them_mo_phan("c1", "9999, Nghĩa trang X", sample_admin, deps))
    assert "⚠️" in fake_channel.last_text


def test_them_mo_phan_denied_for_member(deps, fake_channel, sample_admin, member_user):
    row = deps.family_store.create_member(created_by=sample_admin.id, full_name="A")
    _run(_cmd_them_mo_phan("c1", f"{row['id']}, X", member_user, deps))
    assert "không có quyền" in fake_channel.last_text.lower()


def test_xoa_mo_phan_success(deps, fake_channel, sample_admin):
    row = deps.family_store.create_member(created_by=sample_admin.id, full_name="A")
    rec = deps.burial_store.create_record(
        created_by=sample_admin.id, member_id=row["id"], cemetery_name="X",
    )
    _run(_cmd_xoa_mo_phan("c1", str(rec["id"]), sample_admin, deps))
    assert deps.burial_store.get_record(rec["id"])["deleted_at"] is not None


# ── _cmd_tim_mo ───────────────────────────────────────────────────────────────


def test_tim_mo_by_name(deps, fake_channel, sample_admin, member_user):
    row = deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn A", alias_name="Ông Nội",
    )
    deps.burial_store.create_record(
        created_by=sample_admin.id, member_id=row["id"],
        cemetery_name="Nghĩa trang Văn Điển", address="Thanh Trì, Hà Nội",
        lat=20.9456, lng=105.8231, plot_info="Lô B3",
    )
    _run(_cmd_tim_mo("c1", "ong noi", member_user, deps))
    text = fake_channel.last_text
    assert "Nghĩa trang Văn Điển" in text
    assert "Thanh Trì" in text
    assert "https://maps.google.com/?q=20.9456,105.8231" in text
    assert "Lô B3" in text


def test_tim_mo_member_without_burial(deps, fake_channel, sample_admin, member_user):
    deps.family_store.create_member(created_by=sample_admin.id, full_name="Nguyễn Văn A")
    _run(_cmd_tim_mo("c1", "van a", member_user, deps))
    assert "chưa có" in fake_channel.last_text.lower()


def test_tim_mo_no_match(deps, fake_channel, member_user):
    _run(_cmd_tim_mo("c1", "khong ton tai", member_user, deps))
    assert "không tìm thấy" in fake_channel.last_text.lower()


def test_tim_mo_multiple_matches_lists_candidates(deps, fake_channel, sample_admin, member_user):
    deps.family_store.create_member(created_by=sample_admin.id, full_name="Nguyễn Văn A")
    deps.family_store.create_member(created_by=sample_admin.id, full_name="Nguyễn Văn Anh")
    _run(_cmd_tim_mo("c1", "nguyen van", member_user, deps))
    text = fake_channel.last_text
    assert "Nguyễn Văn A" in text
    assert "Nguyễn Văn Anh" in text


# ── Wiring: handle_message dispatches family commands ─────────────────────────


@pytest.fixture()
def wired_deps(deps, store):
    deps.user_store = store
    deps.notification_service = None
    return deps


def _make_msg(text: str):
    from interfaces import ChannelMessage
    return ChannelMessage(chat_id="chat1", text=text, channel="telegram", raw={})


def _admin_user(sample_admin):
    return sample_admin


def test_wiring_them_nguoi_than_creates_member(wired_deps, sample_admin):
    from core_handler import handle_message
    _run(handle_message(
        _make_msg("them nguoi than: Nguyễn Văn A, doi 3"), sample_admin, wired_deps,
    ))
    rows = wired_deps.family_store.list_members()
    assert len(rows) == 1
    assert rows[0]["full_name"] == "Nguyễn Văn A"


def test_wiring_tim_mo_dispatches(wired_deps, sample_admin, fake_channel):
    from core_handler import handle_message
    row = wired_deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn A",
    )
    wired_deps.burial_store.create_record(
        created_by=sample_admin.id, member_id=row["id"],
        cemetery_name="Nghĩa trang Văn Điển", lat=20.9, lng=105.8,
    )
    _run(handle_message(_make_msg("tìm mộ van a"), sample_admin, wired_deps))
    assert "Nghĩa trang Văn Điển" in fake_channel.last_text


def test_wiring_danh_sach_nguoi_than_dispatches(wired_deps, sample_admin, fake_channel):
    from core_handler import handle_message
    wired_deps.family_store.create_member(created_by=sample_admin.id, full_name="Cụ Tổ")
    _run(handle_message(_make_msg("danh sach nguoi than"), sample_admin, wired_deps))
    assert "Cụ Tổ" in fake_channel.last_text


def test_wiring_xem_nguoi_than_dispatches(wired_deps, sample_admin, fake_channel):
    from core_handler import handle_message
    row = wired_deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn A",
    )
    _run(handle_message(_make_msg(f"xem nguoi than {row['id']}"), sample_admin, wired_deps))
    assert "Nguyễn Văn A" in fake_channel.last_text


def test_wiring_them_mo_phan_dispatches(wired_deps, sample_admin):
    from core_handler import handle_message
    row = wired_deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn A",
    )
    _run(handle_message(
        _make_msg(f"them mo phan: {row['id']}, Nghĩa trang quê"), sample_admin, wired_deps,
    ))
    assert wired_deps.burial_store.get_current_for_member(row["id"]) is not None


def test_wiring_xoa_nguoi_than_dispatches(wired_deps, sample_admin):
    from core_handler import handle_message
    row = wired_deps.family_store.create_member(
        created_by=sample_admin.id, full_name="Nguyễn Văn A",
    )
    _run(handle_message(_make_msg(f"xoa nguoi than: {row['id']}"), sample_admin, wired_deps))
    assert wired_deps.family_store.get_member(row["id"])["deleted_at"] is not None
