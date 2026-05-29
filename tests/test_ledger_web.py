"""Tests for FR-9 ledger web routes."""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from budget_store import SqliteBudgetStore
from category_store import SqliteCategoryStore
from interfaces import User
from ledger_reports import LedgerReports
from ledger_store import SqliteLedgerStore
from web_channel import WebChannelAdapter
from web_session_store import SqliteWebSessionStore
from web_router import init_web_router, router

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")


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


def _insert_session(conn: sqlite3.Connection, user_id: int, token: str) -> None:
    conn.execute(
        "INSERT INTO web_sessions (user_id, token, created_at, expires_at) "
        "VALUES (?, ?, datetime('now'), datetime('now', '+7 days'))",
        (user_id, token),
    )
    conn.commit()


def _build_client(
    user: User,
) -> tuple[TestClient, sqlite3.Connection, SqliteLedgerStore, SqliteCategoryStore, SqliteBudgetStore]:
    conn = _make_db()
    conn.execute(
        "INSERT INTO users (id, name, username, role) VALUES (?,?,?,?)",
        (user.id, user.name, user.username, user.role),
    )
    conn.commit()

    session_store = SqliteWebSessionStore(ttl_days=7)
    session_store._conn = conn

    user_store = MagicMock()
    user_store.get_user_by_id.return_value = user
    user_store.get_must_change_password = MagicMock(return_value=False)

    elevation_store = MagicMock()
    elevation_store.get_active_session.return_value = None

    conv_store = MagicMock()
    conv_store.list_for_user.return_value = []

    audit = MagicMock()
    web_ch = WebChannelAdapter()
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    ledger_store = SqliteLedgerStore(conn=conn)
    category_store = SqliteCategoryStore(conn=conn)
    budget_store = SqliteBudgetStore(conn=conn)
    ledger_reports = LedgerReports(ledger_store, budget_store)

    init_web_router(
        templates=templates,
        web_channel=web_ch,
        session_store=session_store,
        user_store=user_store,
        audit=audit,
        elevation_store=elevation_store,
        conv_store=conv_store,
        task_store=None,
        anniversary_store=None,
        anniversary_engine=None,
        ledger_store=ledger_store,
        category_store=category_store,
        budget_store=budget_store,
        ledger_reports=ledger_reports,
    )

    app = FastAPI()
    app.include_router(router)
    app.state.web_deps = MagicMock()
    client = TestClient(app, follow_redirects=False)
    return client, conn, ledger_store, category_store, budget_store


@pytest.fixture()
def alice() -> User:
    return User(id=1, name="Alice", role="member", username="alice")


# ── GET /ledger ────────────────────────────────────────────────────────────────


def test_ledger_list_requires_auth(alice):
    client, _, _, _, _ = _build_client(alice)
    r = client.get("/ledger")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")


def test_ledger_list_renders_empty(alice):
    client, conn, _, _, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t1")
    r = client.get("/ledger", cookies={"web_session": "t1"})
    assert r.status_code == 200
    assert "ledger" in r.text.lower() or "chi tiêu" in r.text.lower() or "ghi chép" in r.text.lower()


def test_ledger_list_renders_entries(alice):
    client, conn, ledger_store, category_store, _ = _build_client(alice)
    category_store.create_category("Ăn uống", "expense", user_id=alice.id)
    ledger_store.add_entry(
        alice.id, "expense", 50000, "2026-05-01 12:00:00",
        note="ăn trưa", source="web",
    )
    _insert_session(conn, alice.id, "t2")
    r = client.get("/ledger", cookies={"web_session": "t2"})
    assert r.status_code == 200
    assert "ăn trưa" in r.text


def test_ledger_list_excludes_other_user_entries(alice):
    client, conn, ledger_store, _, _ = _build_client(alice)
    conn.execute("INSERT INTO users (id, name, role) VALUES (999, 'Other', 'member')")
    conn.commit()
    ledger_store.add_entry(999, "expense", 999999, "2026-05-01 00:00:00", note="secret", source="web")
    _insert_session(conn, alice.id, "t3")
    r = client.get("/ledger", cookies={"web_session": "t3"})
    assert "secret" not in r.text


# ── GET /ledger/new ────────────────────────────────────────────────────────────


def test_ledger_new_form_renders(alice):
    client, conn, _, _, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t4")
    r = client.get("/ledger/new", cookies={"web_session": "t4"})
    assert r.status_code == 200
    assert "<form" in r.text


# ── POST /ledger ───────────────────────────────────────────────────────────────


def test_ledger_create_expense_redirects(alice):
    client, conn, ledger_store, _, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t5")
    r = client.post(
        "/ledger",
        data={"kind": "expense", "amount": "50000", "note": "cà phê", "occurred_at": "2026-05-01T08:00"},
        cookies={"web_session": "t5"},
    )
    assert r.status_code in (302, 303)
    entries = ledger_store.list_for_user(alice.id)
    assert len(entries) == 1
    assert entries[0]["note"] == "cà phê"
    assert entries[0]["kind"] == "expense"


def test_ledger_create_income_redirects(alice):
    client, conn, ledger_store, _, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t6")
    r = client.post(
        "/ledger",
        data={"kind": "income", "amount": "5000000", "note": "lương", "occurred_at": "2026-05-05T09:00"},
        cookies={"web_session": "t6"},
    )
    assert r.status_code in (302, 303)
    entries = ledger_store.list_for_user(alice.id)
    assert entries[0]["kind"] == "income"


def test_ledger_create_missing_amount_returns_form(alice):
    client, conn, _, _, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t7")
    r = client.post(
        "/ledger",
        data={"kind": "expense", "amount": "", "note": "test", "occurred_at": "2026-05-01T08:00"},
        cookies={"web_session": "t7"},
    )
    assert r.status_code in (200, 400)
    assert "<form" in r.text


# ── POST /ledger/{id}/void ────────────────────────────────────────────────────


def test_ledger_void_entry(alice):
    client, conn, ledger_store, _, _ = _build_client(alice)
    entry = ledger_store.add_entry(alice.id, "expense", 10000, "2026-05-01 10:00:00", source="web")
    _insert_session(conn, alice.id, "t8")
    r = client.post(
        f"/ledger/{entry['id']}/void",
        cookies={"web_session": "t8"},
    )
    assert r.status_code in (302, 303)
    refreshed = ledger_store.get_entry(entry["id"])
    assert refreshed["voided_at"] is not None


def test_ledger_void_other_user_forbidden(alice):
    client, conn, ledger_store, _, _ = _build_client(alice)
    conn.execute("INSERT INTO users (id, name, role) VALUES (999, 'Other', 'member')")
    conn.commit()
    entry = ledger_store.add_entry(999, "expense", 10000, "2026-05-01 10:00:00", source="web")
    _insert_session(conn, alice.id, "t9")
    r = client.post(f"/ledger/{entry['id']}/void", cookies={"web_session": "t9"})
    assert r.status_code in (403, 404)
    refreshed = ledger_store.get_entry(entry["id"])
    assert refreshed["voided_at"] is None


# ── GET /ledger/categories ─────────────────────────────────────────────────────


def test_ledger_categories_requires_auth(alice):
    client, _, _, _, _ = _build_client(alice)
    r = client.get("/ledger/categories")
    assert r.status_code in (302, 303)


def test_ledger_categories_renders(alice):
    client, conn, _, category_store, _ = _build_client(alice)
    category_store.create_category("Ăn uống", "expense", user_id=alice.id)
    _insert_session(conn, alice.id, "t10")
    r = client.get("/ledger/categories", cookies={"web_session": "t10"})
    assert r.status_code == 200
    assert "Ăn uống" in r.text


# ── POST /ledger/categories ────────────────────────────────────────────────────


def test_ledger_create_category(alice):
    client, conn, _, category_store, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t11")
    r = client.post(
        "/ledger/categories",
        data={"name": "Di chuyển", "kind": "expense"},
        cookies={"web_session": "t11"},
    )
    assert r.status_code in (302, 303)
    cats = category_store.list_for_user(alice.id)
    assert any(c["name"] == "Di chuyển" for c in cats)


# ── POST /ledger/categories/{id}/delete ────────────────────────────────────────


def test_delete_own_category(alice):
    client, conn, _, category_store, _ = _build_client(alice)
    cat = category_store.create_category("Di chuyển", "expense", user_id=alice.id)
    _insert_session(conn, alice.id, "tc1")
    r = client.post(f"/ledger/categories/{cat['id']}/delete", cookies={"web_session": "tc1"})
    assert r.status_code in (302, 303)
    assert category_store.get_category(cat["id"])["deleted_at"] is not None


def test_admin_can_delete_shared_category():
    admin = User(id=1, name="Admin", role="admin", username="admin")
    client, conn, _, category_store, _ = _build_client(admin)
    shared = category_store.create_category("Đi lại", "expense", user_id=None)
    _insert_session(conn, admin.id, "tc2")
    r = client.post(f"/ledger/categories/{shared['id']}/delete", cookies={"web_session": "tc2"})
    assert r.status_code in (302, 303)
    assert category_store.get_category(shared["id"])["deleted_at"] is not None


def test_member_cannot_delete_shared_category(alice):
    client, conn, _, category_store, _ = _build_client(alice)
    shared = category_store.create_category("Đi lại", "expense", user_id=None)
    _insert_session(conn, alice.id, "tc3")
    r = client.post(f"/ledger/categories/{shared['id']}/delete", cookies={"web_session": "tc3"})
    assert r.status_code == 403
    assert category_store.get_category(shared["id"])["deleted_at"] is None


# ── GET /ledger/report ────────────────────────────────────────────────────────


def test_ledger_report_renders(alice):
    client, conn, ledger_store, _, _ = _build_client(alice)
    ledger_store.add_entry(alice.id, "expense", 100000, "2026-05-10 12:00:00", source="web")
    ledger_store.add_entry(alice.id, "income", 500000, "2026-05-15 09:00:00", source="web")
    _insert_session(conn, alice.id, "t12")
    r = client.get("/ledger/report?month=2026-05", cookies={"web_session": "t12"})
    assert r.status_code == 200
    assert "2026-05" in r.text or "Tháng" in r.text


# ── GET /ledger/budget ────────────────────────────────────────────────────────


def test_ledger_budget_renders_no_budget(alice):
    client, conn, _, _, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t13")
    r = client.get("/ledger/budget", cookies={"web_session": "t13"})
    assert r.status_code == 200
    assert "<form" in r.text


def test_ledger_budget_set_redirects(alice):
    client, conn, _, _, budget_store = _build_client(alice)
    _insert_session(conn, alice.id, "t14")
    r = client.post(
        "/ledger/budget",
        data={"month": "2026-05", "expense_budget": "3000000", "savings_target": "1000000"},
        cookies={"web_session": "t14"},
    )
    assert r.status_code in (302, 303)
    budget = budget_store.get_budget(alice.id, "2026-05")
    assert budget is not None
    assert budget["expense_budget"] == 3000000
