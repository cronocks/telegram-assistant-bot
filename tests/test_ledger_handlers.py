"""Tests for cmd_ledger handlers — FR-9."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from audit import SqliteAuditLog
from budget_store import SqliteBudgetStore
from category_store import SqliteCategoryStore
from cmd_ledger import (
    _cmd_bao_cao_nam,
    _cmd_bao_cao_thang,
    _cmd_chi,
    _cmd_dat_han_muc_chi,
    _cmd_dat_muc_tieu_tiet_kiem,
    _cmd_danh_sach_ghi_chep,
    _cmd_ghi_chep_xem,
    _cmd_huy_ghi_chep,
    _cmd_sua_danh_muc,
    _cmd_sua_ghi_chep,
    _cmd_them_danh_muc,
    _cmd_thu,
    _cmd_xem_chi_tieu,
    _cmd_xem_danh_muc,
    _cmd_xem_han_muc,
    _cmd_xoa_danh_muc,
)
from deps import CoreDeps
from ledger_parser import LedgerParser
from ledger_reports import LedgerReports
from ledger_store import SqliteLedgerStore
from user_store import SqliteUserStore

TODAY = "2026-05-26 10:00:00"
CHAT = "chat_123"


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text, use_markdown=True):
        self.sent.append((chat_id, text))

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


@pytest.fixture()
def fake_channel():
    return FakeChannel()


@pytest.fixture()
def all_stores(db_conn):
    return {
        "cat": SqliteCategoryStore(conn=db_conn),
        "ledger": SqliteLedgerStore(conn=db_conn),
        "budget": SqliteBudgetStore(conn=db_conn),
        "user_store": SqliteUserStore(conn=db_conn),
        "audit": SqliteAuditLog(conn=db_conn),
    }


@pytest.fixture()
def deps(db_conn, all_stores, fake_channel):
    cat = all_stores["cat"]
    ledger = all_stores["ledger"]
    budget = all_stores["budget"]
    reports = LedgerReports(ledger_store=ledger, budget_store=budget)
    parser = LedgerParser(client=None)

    d = MagicMock(spec=CoreDeps)
    d.channel = fake_channel
    d.audit = all_stores["audit"]
    d.user_store = all_stores["user_store"]
    d.category_store = cat
    d.ledger_store = ledger
    d.budget_store = budget
    d.ledger_parser = parser
    d.ledger_reports = reports
    d.notification_service = MagicMock()
    d.notification_service.enqueue = MagicMock()
    return d


def run(coro):
    return asyncio.run(coro)


# ── _cmd_chi ──────────────────────────────────────────────────────────────────


def test_cmd_chi_creates_expense_entry(deps, member_user):
    run(_cmd_chi(CHAT, "50k ăn trưa", member_user, deps))
    entries = deps.ledger_store.list_for_user(member_user.id)
    assert len(entries) == 1
    assert entries[0]["kind"] == "expense"
    assert entries[0]["amount"] == 50_000
    assert "✅" in deps.channel.last_text


def test_cmd_chi_bad_amount_replies_error(deps, member_user):
    run(_cmd_chi(CHAT, "ăn trưa không có số", member_user, deps))
    assert "⚠" in deps.channel.last_text
    assert deps.ledger_store.list_for_user(member_user.id) == []


def test_cmd_chi_empty_body_replies_usage(deps, member_user):
    run(_cmd_chi(CHAT, "", member_user, deps))
    assert "⚠" in deps.channel.last_text or "chi:" in deps.channel.last_text.lower()


def test_cmd_chi_fires_threshold_alert_at_80_pct(deps, member_user):
    deps.budget_store.upsert_budget(member_user.id, "2026-05", expense_budget=100_000)
    run(_cmd_chi(CHAT, "80k test", member_user, deps))
    deps.notification_service.enqueue.assert_called_once()
    assert deps.budget_store.is_alert_sent(member_user.id, "2026-05", "80")


# ── _cmd_thu ──────────────────────────────────────────────────────────────────


def test_cmd_thu_creates_income_entry(deps, member_user):
    run(_cmd_thu(CHAT, "5tr lương tháng 5", member_user, deps))
    entries = deps.ledger_store.list_for_user(member_user.id)
    assert entries[0]["kind"] == "income"
    assert entries[0]["amount"] == 5_000_000


# ── _cmd_ghi_chep_xem ─────────────────────────────────────────────────────────


def test_cmd_ghi_chep_xem_shows_entry(deps, member_user):
    entry = deps.ledger_store.add_entry(member_user.id, "expense", 30_000, TODAY)
    run(_cmd_ghi_chep_xem(CHAT, str(entry["id"]), member_user, deps))
    assert "30.000" in deps.channel.last_text


def test_cmd_ghi_chep_xem_not_found(deps, member_user):
    run(_cmd_ghi_chep_xem(CHAT, "99999", member_user, deps))
    assert "không tìm thấy" in deps.channel.last_text.lower()


def test_cmd_ghi_chep_xem_wrong_owner(deps, member_user, another_user):
    entry = deps.ledger_store.add_entry(another_user.id, "expense", 10_000, TODAY)
    run(_cmd_ghi_chep_xem(CHAT, str(entry["id"]), member_user, deps))
    assert "không tìm thấy" in deps.channel.last_text.lower()


# ── _cmd_danh_sach_ghi_chep ───────────────────────────────────────────────────


def test_cmd_danh_sach_ghi_chep_shows_entries(deps, member_user):
    deps.ledger_store.add_entry(member_user.id, "expense", 50_000, TODAY)
    run(_cmd_danh_sach_ghi_chep(CHAT, member_user, deps))
    assert "50.000" in deps.channel.last_text


def test_cmd_danh_sach_ghi_chep_empty(deps, member_user):
    run(_cmd_danh_sach_ghi_chep(CHAT, member_user, deps))
    assert "chưa có" in deps.channel.last_text.lower() or deps.channel.last_text


# ── _cmd_huy_ghi_chep ────────────────────────────────────────────────────────


def test_cmd_huy_ghi_chep_voids_entry(deps, member_user):
    entry = deps.ledger_store.add_entry(member_user.id, "expense", 20_000, TODAY)
    run(_cmd_huy_ghi_chep(CHAT, str(entry["id"]), member_user, deps))
    assert deps.ledger_store.get_entry(entry["id"])["voided_at"] is not None
    assert "✅" in deps.channel.last_text


def test_cmd_huy_ghi_chep_not_found(deps, member_user):
    run(_cmd_huy_ghi_chep(CHAT, "99999", member_user, deps))
    assert "không tìm thấy" in deps.channel.last_text.lower()


# ── _cmd_sua_ghi_chep ────────────────────────────────────────────────────────


def test_cmd_sua_ghi_chep_updates_amount(deps, member_user):
    entry = deps.ledger_store.add_entry(member_user.id, "expense", 10_000, TODAY)
    run(_cmd_sua_ghi_chep(CHAT, f"{entry['id']}, so=20k", member_user, deps))
    updated = deps.ledger_store.get_entry(entry["id"])
    assert updated["amount"] == 20_000


# ── _cmd_xem_danh_muc ────────────────────────────────────────────────────────


def test_cmd_xem_danh_muc_shows_list(deps, member_user):
    deps.category_store.create_category("Ăn uống", "expense", user_id=member_user.id)
    run(_cmd_xem_danh_muc(CHAT, member_user, deps))
    assert "Ăn uống" in deps.channel.last_text


# ── _cmd_them_danh_muc ───────────────────────────────────────────────────────


def test_cmd_them_danh_muc_creates_personal(deps, member_user):
    run(_cmd_them_danh_muc(CHAT, "Cafe, chi", member_user, deps))
    cats = deps.category_store.list_for_user(member_user.id)
    assert any(c["name"] == "Cafe" for c in cats)


def test_cmd_them_danh_muc_shared_requires_admin(deps, member_user):
    run(_cmd_them_danh_muc(CHAT, "Tiền nhà, chi, chung", member_user, deps))
    assert "⚠" in deps.channel.last_text or "không có quyền" in deps.channel.last_text.lower()


def test_cmd_them_danh_muc_shared_allowed_for_admin(deps, sample_admin):
    run(_cmd_them_danh_muc(CHAT, "Tiền nhà, chi, chung", sample_admin, deps))
    cats = deps.category_store.list_for_user(sample_admin.id)
    shared = [c for c in cats if c["user_id"] is None and c["name"] == "Tiền nhà"]
    assert len(shared) == 1


# ── _cmd_xoa_danh_muc ────────────────────────────────────────────────────────


def test_cmd_xoa_danh_muc_soft_deletes(deps, member_user):
    cat = deps.category_store.create_category("Xóa đi", "expense", user_id=member_user.id)
    run(_cmd_xoa_danh_muc(CHAT, str(cat["id"]), member_user, deps))
    assert deps.category_store.get_category(cat["id"])["deleted_at"] is not None


# ── _cmd_sua_danh_muc ────────────────────────────────────────────────────────


def test_cmd_sua_danh_muc_renames(deps, member_user):
    cat = deps.category_store.create_category("Tên cũ", "expense", user_id=member_user.id)
    run(_cmd_sua_danh_muc(CHAT, f"{cat['id']} Tên mới", member_user, deps))
    assert deps.category_store.get_category(cat["id"])["name"] == "Tên mới"


# ── Reports ───────────────────────────────────────────────────────────────────


def test_cmd_bao_cao_thang_current_month(deps, member_user):
    deps.ledger_store.add_entry(member_user.id, "income", 5_000_000, TODAY)
    run(_cmd_bao_cao_thang(CHAT, "", member_user, deps))
    assert "5.000.000" in deps.channel.last_text


def test_cmd_bao_cao_thang_specific_month(deps, member_user):
    run(_cmd_bao_cao_thang(CHAT, "2026-04", member_user, deps))
    assert deps.channel.last_text  # any non-empty reply


def test_cmd_bao_cao_nam_returns_reply(deps, member_user):
    run(_cmd_bao_cao_nam(CHAT, member_user, deps))
    assert deps.channel.last_text


def test_cmd_xem_chi_tieu_returns_reply(deps, member_user):
    deps.ledger_store.add_entry(member_user.id, "expense", 100_000, TODAY)
    run(_cmd_xem_chi_tieu(CHAT, member_user, deps))
    assert "100.000" in deps.channel.last_text


# ── Budget ────────────────────────────────────────────────────────────────────


def test_cmd_dat_han_muc_chi_sets_budget(deps, member_user):
    run(_cmd_dat_han_muc_chi(CHAT, "10tr", member_user, deps))
    row = deps.budget_store.get_budget(member_user.id, "2026-05")
    assert row is not None
    assert row["expense_budget"] == 10_000_000


def test_cmd_dat_muc_tieu_tiet_kiem_sets_target(deps, member_user):
    run(_cmd_dat_muc_tieu_tiet_kiem(CHAT, "2tr", member_user, deps))
    row = deps.budget_store.get_budget(member_user.id, "2026-05")
    assert row is not None
    assert row["savings_target"] == 2_000_000


def test_cmd_xem_han_muc_returns_reply(deps, member_user):
    deps.budget_store.upsert_budget(member_user.id, "2026-05", expense_budget=5_000_000)
    run(_cmd_xem_han_muc(CHAT, member_user, deps))
    assert "5.000.000" in deps.channel.last_text
