"""Tests for FR-8 cmd_anniversary handlers."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from anniversary_engine import AnniversaryEngine
from anniversary_store import SqliteAnniversaryStore
from audit import SqliteAuditLog
from cmd_anniversary import (
    ParseAnniversaryError,
    _cmd_danh_sach_ky_niem,
    _cmd_sua_ky_niem,
    _cmd_them_ky_niem,
    _cmd_xem_ky_niem,
    _cmd_xoa_ky_niem,
    parse_anniversary_id,
    parse_anniversary_input,
)
from deps import CoreDeps


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text, use_markdown=True):
        self.sent.append((chat_id, text))

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


@pytest.fixture()
def anniv_store(db_conn):
    return SqliteAnniversaryStore(conn=db_conn)


@pytest.fixture()
def fake_channel():
    return FakeChannel()


@pytest.fixture()
def deps(db_conn, anniv_store, store, fake_channel):
    audit = SqliteAuditLog(conn=db_conn)
    engine = AnniversaryEngine(
        anniv_store=anniv_store,
        user_store=store,
        notification_service=MagicMock(),
        audit=audit,
        conn=db_conn,
    )
    d = MagicMock(spec=CoreDeps)
    d.channel = fake_channel
    d.user_store = store
    d.anniversary_store = anniv_store
    d.anniversary_engine = engine
    d.audit = audit
    return d


def _run(coro):
    return asyncio.run(coro)


# ── parse_anniversary_input ───────────────────────────────────────────────────


def test_parse_full_lunar():
    p = parse_anniversary_input("Giỗ ông nội, âm 10/3, giỗ")
    assert p == {
        "name": "Giỗ ông nội", "date_type": "lunar",
        "day": 10, "month": 3, "category": "gio", "is_leap_month": 0,
    }


def test_parse_full_solar():
    p = parse_anniversary_input("Kỷ niệm cưới, dương 15/8, cưới")
    assert p["name"] == "Kỷ niệm cưới"
    assert p["date_type"] == "solar"
    assert p["day"] == 15
    assert p["month"] == 8
    assert p["category"] == "cuoi"


def test_parse_accepts_unaccented():
    """User may type without diacritics."""
    p = parse_anniversary_input("Gio ong, am 10/3, gio")
    assert p["date_type"] == "lunar"
    assert p["category"] == "gio"


def test_parse_default_category():
    p = parse_anniversary_input("Sinh nhật mẹ, âm 5/10")
    assert p["category"] == "khac"


def test_parse_rejects_missing_date():
    with pytest.raises(ParseAnniversaryError):
        parse_anniversary_input("Giỗ ông nội")


def test_parse_rejects_invalid_date_type():
    with pytest.raises(ParseAnniversaryError):
        parse_anniversary_input("X, hebrew 1/1")


def test_parse_rejects_bad_date_format():
    with pytest.raises(ParseAnniversaryError):
        parse_anniversary_input("X, âm abc/def")


def test_parse_rejects_out_of_range():
    with pytest.raises(ParseAnniversaryError):
        parse_anniversary_input("X, dương 32/1")


def test_parse_rejects_invalid_category():
    with pytest.raises(ParseAnniversaryError):
        parse_anniversary_input("X, dương 1/1, weird")


def test_parse_leap_month_flag():
    p = parse_anniversary_input("Giỗ ông, âm 9/6 nhuận, giỗ")
    assert p["is_leap_month"] == 1
    assert p["date_type"] == "lunar"
    assert p["day"] == 9
    assert p["month"] == 6


def test_parse_no_leap_flag_defaults_zero():
    p = parse_anniversary_input("Giỗ ông, âm 9/6, giỗ")
    assert p["is_leap_month"] == 0


def test_parse_leap_unaccented():
    p = parse_anniversary_input("Gio ong, am 9/6 nhuan")
    assert p["is_leap_month"] == 1


def test_parse_solar_ignores_nhuan():
    # "nhuận" on a solar date is meaningless but should not crash.
    p = parse_anniversary_input("X, dương 9/6 nhuận")
    assert p["is_leap_month"] == 0  # solar always 0


# ── parse_anniversary_id ──────────────────────────────────────────────────────


def test_parse_id_simple():
    assert parse_anniversary_id("42") == 42
    assert parse_anniversary_id("  7  ") == 7


def test_parse_id_invalid():
    assert parse_anniversary_id("abc") is None
    assert parse_anniversary_id("") is None


# ── _cmd_them_ky_niem ─────────────────────────────────────────────────────────


def test_them_ky_niem_creates_row(deps, member_user, anniv_store, fake_channel):
    _run(_cmd_them_ky_niem(
        "1", "Giỗ ông nội, âm 10/3, giỗ", member_user, deps,
    ))
    rows = anniv_store.list_for_user(member_user.id)
    assert len(rows) == 1
    assert rows[0]["name"] == "Giỗ ông nội"
    assert rows[0]["date_type"] == "lunar"
    assert "Giỗ ông nội" in fake_channel.last_text


def test_them_ky_niem_reports_parse_error(deps, member_user, fake_channel):
    _run(_cmd_them_ky_niem("1", "Invalid input", member_user, deps))
    # No row created, error message sent.
    assert fake_channel.sent  # something was sent
    assert len(deps.anniversary_store.list_for_user(member_user.id)) == 0


def test_them_ky_niem_empty_body_shows_help(deps, member_user, fake_channel):
    _run(_cmd_them_ky_niem("1", "", member_user, deps))
    assert "ví dụ" in fake_channel.last_text.lower() or "vd" in fake_channel.last_text.lower()


# ── _cmd_danh_sach_ky_niem ────────────────────────────────────────────────────


def test_danh_sach_empty(deps, member_user, fake_channel):
    _run(_cmd_danh_sach_ky_niem("1", member_user, deps))
    assert "chưa có" in fake_channel.last_text.lower() or "không có" in fake_channel.last_text.lower()


def test_danh_sach_lists_user_rows(deps, member_user, anniv_store, fake_channel):
    anniv_store.create_anniversary(
        user_id=member_user.id, name="Giỗ ông", date_type="lunar", month=3, day=10,
    )
    anniv_store.create_anniversary(
        user_id=member_user.id, name="Cưới", date_type="solar", month=8, day=15,
    )
    _run(_cmd_danh_sach_ky_niem("1", member_user, deps))
    assert "Giỗ ông" in fake_channel.last_text
    assert "Cưới" in fake_channel.last_text


# ── _cmd_xem_ky_niem ──────────────────────────────────────────────────────────


def test_xem_ky_niem_shows_detail(deps, member_user, anniv_store, fake_channel):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="Giỗ ông", date_type="lunar", month=3, day=10,
    )
    _run(_cmd_xem_ky_niem("1", str(a["id"]), member_user, deps))
    assert "Giỗ ông" in fake_channel.last_text


def test_xem_ky_niem_not_found(deps, member_user, fake_channel):
    _run(_cmd_xem_ky_niem("1", "999", member_user, deps))
    assert "không tìm thấy" in fake_channel.last_text.lower()


def test_xem_ky_niem_owner_only(deps, member_user, another_user, anniv_store, fake_channel):
    a = anniv_store.create_anniversary(
        user_id=another_user.id, name="Private", date_type="lunar", month=1, day=1,
    )
    _run(_cmd_xem_ky_niem("1", str(a["id"]), member_user, deps))
    # Should not leak — either "không tìm thấy" or permission denied.
    assert "Private" not in fake_channel.last_text


# ── _cmd_xoa_ky_niem ──────────────────────────────────────────────────────────


def test_xoa_ky_niem_soft_deletes(deps, member_user, anniv_store, fake_channel):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="X", date_type="lunar", month=1, day=1,
    )
    _run(_cmd_xoa_ky_niem("1", str(a["id"]), member_user, deps))
    row = anniv_store.get_anniversary(a["id"])
    assert row["deleted_at"] is not None


def test_xoa_ky_niem_not_owner(deps, member_user, another_user, anniv_store, fake_channel):
    a = anniv_store.create_anniversary(
        user_id=another_user.id, name="X", date_type="lunar", month=1, day=1,
    )
    _run(_cmd_xoa_ky_niem("1", str(a["id"]), member_user, deps))
    # Should not delete.
    row = anniv_store.get_anniversary(a["id"])
    assert row["deleted_at"] is None


# ── _cmd_sua_ky_niem ──────────────────────────────────────────────────────────


def test_sua_ky_niem_renames(deps, member_user, anniv_store, fake_channel):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="Old name", date_type="lunar", month=1, day=1,
    )
    _run(_cmd_sua_ky_niem("1", f"{a['id']}, ten=New name", member_user, deps))
    row = anniv_store.get_anniversary(a["id"])
    assert row["name"] == "New name"


def test_sua_ky_niem_changes_date(deps, member_user, anniv_store, fake_channel):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="X", date_type="lunar", month=1, day=1,
    )
    _run(_cmd_sua_ky_niem("1", f"{a['id']}, ngay=dương 15/8", member_user, deps))
    row = anniv_store.get_anniversary(a["id"])
    assert row["date_type"] == "solar"
    assert row["month"] == 8
    assert row["day"] == 15
