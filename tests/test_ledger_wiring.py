"""Smoke tests for FR-9 wiring: handle_message dispatches ledger commands correctly.

Assertions check actual DB state (entries created, categories created, budgets set)
rather than just that channel.send was called — this ensures real dispatch happens,
not the LLM fallback path.
"""
from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from budget_store import SqliteBudgetStore
from category_store import SqliteCategoryStore
from deps import CoreDeps
from interfaces import ChannelMessage, User
from ledger_parser import LedgerParser
from ledger_reports import LedgerReports
from ledger_store import SqliteLedgerStore


def _make_db() -> sqlite3.Connection:
    from db.migrations import run_migrations
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    import db.connection as db_mod
    original = db_mod._conn
    db_mod._conn = conn
    run_migrations()
    db_mod._conn = original
    return conn


def _make_msg(text: str, chat_id: str = "chat1") -> ChannelMessage:
    return ChannelMessage(chat_id=chat_id, text=text, channel="telegram", raw={})


def _make_deps(conn: sqlite3.Connection) -> tuple[CoreDeps, SqliteLedgerStore, SqliteCategoryStore, SqliteBudgetStore]:
    conn.execute("INSERT INTO users (id, name, role) VALUES (1, 'Alice', 'member')")
    conn.commit()

    channel = MagicMock()
    channel.send = AsyncMock()

    ledger_store = SqliteLedgerStore(conn=conn)
    category_store = SqliteCategoryStore(conn=conn)
    budget_store = SqliteBudgetStore(conn=conn)
    ledger_reports = LedgerReports(ledger_store, budget_store)
    ledger_parser = LedgerParser(client=None)  # disable LLM in integration tests

    audit = MagicMock()
    audit.log = MagicMock()

    user_store = MagicMock()
    user_store.get_quota.return_value = None
    user_store.list_users.return_value = []

    deps = MagicMock(spec=CoreDeps)
    deps.channel = channel
    deps.ledger_store = ledger_store
    deps.category_store = category_store
    deps.budget_store = budget_store
    deps.ledger_reports = ledger_reports
    deps.ledger_parser = ledger_parser
    deps.audit = audit
    deps.user_store = user_store
    deps.notification_service = None
    deps.reminder_engine = None
    deps.anniversary_engine = None

    return deps, ledger_store, category_store, budget_store


@pytest.fixture()
def conn():
    return _make_db()


@pytest.fixture()
def alice() -> User:
    return User(id=1, name="Alice", role="member", username="alice")


def _run(coro):
    return asyncio.run(coro)


# ── Entry dispatch ────────────────────────────────────────────────────────────

def test_dispatch_chi_creates_entry(conn, alice):
    from core_handler import handle_message
    deps, ledger_store, _, _ = _make_deps(conn)
    msg = _make_msg("chi: 50000 ăn trưa")
    _run(handle_message(msg, alice, deps))
    entries = ledger_store.list_for_user(alice.id)
    assert len(entries) == 1
    assert entries[0]["kind"] == "expense"
    assert entries[0]["amount"] == 50000


def test_dispatch_thu_creates_income_entry(conn, alice):
    from core_handler import handle_message
    deps, ledger_store, _, _ = _make_deps(conn)
    msg = _make_msg("thu: 2000000 lương")
    _run(handle_message(msg, alice, deps))
    entries = ledger_store.list_for_user(alice.id)
    assert len(entries) == 1
    assert entries[0]["kind"] == "income"
    assert entries[0]["amount"] == 2000000


def test_dispatch_huy_ghi_chep_voids_entry(conn, alice):
    from core_handler import handle_message
    deps, ledger_store, _, _ = _make_deps(conn)
    entry = ledger_store.add_entry(alice.id, "expense", 10000, "2026-05-01 10:00:00", source="telegram")
    msg = _make_msg(f"huy ghi chep: {entry['id']}")
    _run(handle_message(msg, alice, deps))
    refreshed = ledger_store.get_entry(entry["id"])
    assert refreshed["voided_at"] is not None


# ── Category dispatch ─────────────────────────────────────────────────────────

def test_dispatch_them_danh_muc_creates_category(conn, alice):
    from core_handler import handle_message
    deps, _, category_store, _ = _make_deps(conn)
    msg = _make_msg("them danh muc: Ăn uống, chi")
    _run(handle_message(msg, alice, deps))
    cats = category_store.list_for_user(alice.id)
    assert any(c["name"] == "Ăn uống" for c in cats)


def test_dispatch_xoa_danh_muc_soft_deletes(conn, alice):
    from core_handler import handle_message
    deps, _, category_store, _ = _make_deps(conn)
    cat = category_store.create_category("Di chuyển", "expense", user_id=alice.id)
    msg = _make_msg(f"xoa danh muc: {cat['id']}")
    _run(handle_message(msg, alice, deps))
    refreshed = category_store.get_category(cat["id"])
    assert refreshed["deleted_at"] is not None


# ── Budget dispatch ───────────────────────────────────────────────────────────

def test_dispatch_dat_han_muc_chi_sets_budget(conn, alice):
    from core_handler import handle_message
    deps, _, _, budget_store = _make_deps(conn)
    msg = _make_msg("dat han muc chi: 5000000")
    _run(handle_message(msg, alice, deps))
    from timeutils import VIETNAM_TZ
    from datetime import datetime
    month = datetime.now(VIETNAM_TZ).strftime("%Y-%m")
    budget = budget_store.get_budget(alice.id, month)
    assert budget is not None
    assert budget["expense_budget"] == 5000000


def test_dispatch_dat_muc_tieu_tiet_kiem(conn, alice):
    from core_handler import handle_message
    deps, _, _, budget_store = _make_deps(conn)
    msg = _make_msg("dat muc tieu tiet kiem: 1000000")
    _run(handle_message(msg, alice, deps))
    from timeutils import VIETNAM_TZ
    from datetime import datetime
    month = datetime.now(VIETNAM_TZ).strftime("%Y-%m")
    budget = budget_store.get_budget(alice.id, month)
    assert budget is not None
    assert budget["savings_target"] == 1000000


# ── Report / list dispatch (verify send called, not LLM fallback) ─────────────

def test_dispatch_danh_sach_ghi_chep_no_llm(conn, alice):
    """Verifies danh sach ghi chep is dispatched (not LLM fallback)."""
    from core_handler import handle_message
    deps, _, _, _ = _make_deps(conn)
    # LLM mock would fail if called — no extract_search_intent wired
    deps.llm = MagicMock()
    deps.llm.extract_search_intent = MagicMock(side_effect=AssertionError("LLM fallback triggered"))
    msg = _make_msg("danh sach ghi chep")
    _run(handle_message(msg, alice, deps))
    deps.channel.send.assert_awaited()


def test_dispatch_xem_danh_muc_no_llm(conn, alice):
    from core_handler import handle_message
    deps, _, _, _ = _make_deps(conn)
    deps.llm = MagicMock()
    deps.llm.extract_search_intent = MagicMock(side_effect=AssertionError("LLM fallback triggered"))
    msg = _make_msg("xem danh muc")
    _run(handle_message(msg, alice, deps))
    deps.channel.send.assert_awaited()


def test_dispatch_xem_han_muc_no_llm(conn, alice):
    from core_handler import handle_message
    deps, _, _, _ = _make_deps(conn)
    deps.llm = MagicMock()
    deps.llm.extract_search_intent = MagicMock(side_effect=AssertionError("LLM fallback triggered"))
    msg = _make_msg("xem han muc")
    _run(handle_message(msg, alice, deps))
    deps.channel.send.assert_awaited()


def test_ledger_parser_has_llm_client():
    """LedgerParser must self-initialize an Anthropic client (same pattern as TaskParser)."""
    parser = LedgerParser()
    assert parser._client is not None
