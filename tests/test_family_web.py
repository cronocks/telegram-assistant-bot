"""Tests for FR-11 family web routes."""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from burial_store import SqliteBurialStore
from family_store import SqliteFamilyStore
from interfaces import User
from web_channel import WebChannelAdapter
from web_router import init_web_router, router
from web_session_store import SqliteWebSessionStore

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
) -> tuple[TestClient, sqlite3.Connection, SqliteFamilyStore, SqliteBurialStore]:
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

    family_store = SqliteFamilyStore(conn=conn)
    burial_store = SqliteBurialStore(conn=conn)

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
        family_store=family_store,
        burial_store=burial_store,
    )

    app = FastAPI()
    app.include_router(router)
    app.state.web_deps = MagicMock()
    return TestClient(app, follow_redirects=False), conn, family_store, burial_store


@pytest.fixture()
def admin() -> User:
    return User(id=1, name="Admin", role="admin", username="admin")


@pytest.fixture()
def member_user() -> User:
    return User(id=2, name="Member", role="member", username="member")


# ── GET /family/members ───────────────────────────────────────────────────────


def test_family_list_requires_auth(admin):
    client, _, _, _ = _build_client(admin)
    r = client.get("/family/members")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")


def test_family_list_renders_empty(admin):
    client, conn, _, _ = _build_client(admin)
    _insert_session(conn, admin.id, "t1")
    r = client.get("/family/members", cookies={"web_session": "t1"})
    assert r.status_code == 200


def test_family_list_renders_members(admin):
    client, conn, family_store, _ = _build_client(admin)
    family_store.create_member(created_by=admin.id, full_name="Nguyễn Văn A")
    _insert_session(conn, admin.id, "t2")
    r = client.get("/family/members", cookies={"web_session": "t2"})
    assert r.status_code == 200
    assert "Nguyễn Văn A" in r.text


def test_family_list_search_filters(admin):
    client, conn, family_store, _ = _build_client(admin)
    family_store.create_member(created_by=admin.id, full_name="Trần Thị B")
    family_store.create_member(created_by=admin.id, full_name="Nguyễn Văn C")
    _insert_session(conn, admin.id, "t3")
    r = client.get("/family/members?q=Tran", cookies={"web_session": "t3"})
    assert r.status_code == 200
    assert "Trần Thị B" in r.text
    assert "Nguyễn Văn C" not in r.text


# ── GET /family ───────────────────────────────────────────────────────────────


def test_family_tree_requires_auth(admin):
    client, _, _, _ = _build_client(admin)
    r = client.get("/family")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")


def test_family_tree_renders(admin):
    client, conn, _, _ = _build_client(admin)
    _insert_session(conn, admin.id, "t4")
    r = client.get("/family", cookies={"web_session": "t4"})
    assert r.status_code == 200


# ── GET /family/members/new ───────────────────────────────────────────────────


def test_family_member_new_requires_admin(member_user):
    client, conn, _, _ = _build_client(member_user)
    _insert_session(conn, member_user.id, "t5")
    r = client.get("/family/members/new", cookies={"web_session": "t5"})
    assert r.status_code == 403


def test_family_member_new_form_renders(admin):
    client, conn, _, _ = _build_client(admin)
    _insert_session(conn, admin.id, "t6")
    r = client.get("/family/members/new", cookies={"web_session": "t6"})
    assert r.status_code == 200
    assert "<form" in r.text


# ── POST /family/members ──────────────────────────────────────────────────────


def test_family_member_create_redirects(admin):
    client, conn, family_store, _ = _build_client(admin)
    _insert_session(conn, admin.id, "t7")
    r = client.post(
        "/family/members",
        data={"full_name": "Lê Văn D", "gender": "nam"},
        cookies={"web_session": "t7"},
    )
    assert r.status_code in (302, 303)
    rows = family_store.list_members()
    assert len(rows) == 1
    assert rows[0]["full_name"] == "Lê Văn D"


def test_family_member_create_validation_error(admin):
    client, conn, family_store, _ = _build_client(admin)
    _insert_session(conn, admin.id, "t8")
    r = client.post(
        "/family/members",
        data={"full_name": ""},
        cookies={"web_session": "t8"},
    )
    assert r.status_code == 400
    assert len(family_store.list_members()) == 0


# ── GET /family/members/{id} ──────────────────────────────────────────────────


def test_family_member_view_renders(admin):
    client, conn, family_store, _ = _build_client(admin)
    m = family_store.create_member(created_by=admin.id, full_name="Phan Thị E")
    _insert_session(conn, admin.id, "t9")
    r = client.get(f"/family/members/{m['id']}", cookies={"web_session": "t9"})
    assert r.status_code == 200
    assert "Phan Thị E" in r.text


def test_family_member_view_with_burial(admin):
    client, conn, family_store, burial_store = _build_client(admin)
    m = family_store.create_member(created_by=admin.id, full_name="Hoàng Văn F")
    burial_store.create_record(
        created_by=admin.id, member_id=m["id"],
        cemetery_name="Nghĩa trang X",
        address="123 Đường Y",
        lat=10.5,
        lng=106.7,
    )
    _insert_session(conn, admin.id, "t10")
    r = client.get(f"/family/members/{m['id']}", cookies={"web_session": "t10"})
    assert r.status_code == 200
    assert "Nghĩa trang X" in r.text
    assert "maps.google.com" in r.text


def test_family_member_view_not_found(admin):
    client, conn, _, _ = _build_client(admin)
    _insert_session(conn, admin.id, "t11")
    r = client.get("/family/members/9999", cookies={"web_session": "t11"})
    assert r.status_code == 404


# ── GET /family/members/{id}/edit ─────────────────────────────────────────────


def test_family_member_edit_requires_admin(member_user):
    client, conn, family_store, _ = _build_client(member_user)
    m = family_store.create_member(created_by=member_user.id, full_name="Test Member")
    _insert_session(conn, member_user.id, "t12")
    r = client.get(f"/family/members/{m['id']}/edit", cookies={"web_session": "t12"})
    assert r.status_code == 403


# ── POST /family/members/{id} ─────────────────────────────────────────────────


def test_family_member_update_persists(admin):
    client, conn, family_store, _ = _build_client(admin)
    m = family_store.create_member(created_by=admin.id, full_name="Old Name")
    _insert_session(conn, admin.id, "t13")
    r = client.post(
        f"/family/members/{m['id']}",
        data={"full_name": "New Name", "gender": "nu"},
        cookies={"web_session": "t13"},
    )
    assert r.status_code in (302, 303)
    updated = family_store.get_member(m["id"])
    assert updated["full_name"] == "New Name"
