"""tests/test_web_conversation_router.py — Integration tests for conversation routes (FR-5.5).

Covers:
  - /api/conversations (list, search)
  - /api/conversations/{id}/messages (ownership)
  - PATCH /api/conversations/{id} (rename)
  - GET /admin/users, /admin/users/{id}/conversations, /admin/conversations/{id}
"""
import sqlite3
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from interfaces import User
from web_channel import WebChannelAdapter
from web_conversation_store import SqliteWebConversationStore
from web_session_store import SqliteWebSessionStore
from web_router import router, init_web_router


# ── DB / store helpers ─────────────────────────────────────────────────────────

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT,
        role TEXT NOT NULL DEFAULT 'member',
        birthdate DATE,
        password_hash TEXT,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        deleted_at DATETIME
    );
    CREATE TABLE IF NOT EXISTS web_sessions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        token      TEXT NOT NULL UNIQUE,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME NOT NULL,
        revoked_at DATETIME
    );
    CREATE INDEX IF NOT EXISTS idx_web_sessions_token ON web_sessions(token);
    CREATE TABLE IF NOT EXISTS web_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS web_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
"""


def _make_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _make_user_store(conn, *users_with_pw):
    """Stub UserStore; users_with_pw is list of (User, password_hash_or_None)."""
    from auth import hash_password
    user_map = {}
    for user, password in users_with_pw:
        pw_hash = hash_password(password) if password else None
        conn.execute(
            "INSERT INTO users (id, name, username, role, birthdate, password_hash) VALUES (?,?,?,?,?,?)",
            (user.id, user.name, user.username, user.role, str(user.birthdate) if user.birthdate else None, pw_hash),
        )
        user_map[user.id] = (user, pw_hash)
    conn.commit()

    store = MagicMock()
    store.find_by_username_or_name.side_effect = lambda name: next(
        (u for u, _ in user_map.values() if u.username == name or u.name == name), None
    )
    store.get_user_by_id.side_effect = lambda uid: user_map.get(uid, (None, None))[0]
    store.get_password_hash.side_effect = lambda uid: user_map.get(uid, (None, None))[1]
    store.check_password.side_effect = lambda uid, plain: (
        __import__("auth").verify_password(plain, user_map[uid][1]) if uid in user_map and user_map[uid][1] else False
    )
    store.get_must_change_password.return_value = False
    store.set_password.return_value = None
    store.set_must_change_password.return_value = None
    store.list_users.return_value = [u for u, _ in user_map.values()]
    store._conn = conn
    return store


def _make_elevation_store():
    es = MagicMock()
    es.is_locked.return_value = (False, None)
    es.record_failure.return_value = {"locked": False}
    es.reset_failures.return_value = None
    return es


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def alice():
    return User(id=1, name="Alice", role="member", username="alice")


@pytest.fixture()
def admin_user():
    return User(id=10, name="Admin", role="admin", username="admin")


@pytest.fixture()
def minor_child():
    """User under 18 with a birthdate set."""
    return User(id=20, name="Child", role="member", username="child",
                birthdate=date(2015, 6, 1))


def _build_client(conn, user_store, session_store, conv_store, elevation_store, audit):
    import os
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
    templates = Jinja2Templates(directory=templates_dir)
    web_ch = WebChannelAdapter()

    init_web_router(
        templates=templates,
        web_channel=web_ch,
        session_store=session_store,
        user_store=user_store,
        audit=audit,
        elevation_store=elevation_store,
        conv_store=conv_store,
    )

    app = FastAPI()
    app.include_router(router)
    app.state.web_deps = MagicMock()
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def app_client(alice):
    conn = _make_conn()
    user_store = _make_user_store(conn, (alice, "secret123"))
    session_store = SqliteWebSessionStore(ttl_days=7)
    session_store._conn = conn
    conv_store = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
    conv_store._conn = conn
    audit = MagicMock()
    elevation_store = _make_elevation_store()
    return _build_client(conn, user_store, session_store, conv_store, elevation_store, audit)


@pytest.fixture()
def authed_client(app_client):
    """TestClient already logged in as alice."""
    r = app_client.post("/login", data={"username": "alice", "password": "secret123"})
    assert r.status_code == 303
    app_client.cookies.set("web_session", r.cookies["web_session"])
    return app_client


@pytest.fixture()
def admin_ctx(admin_user, minor_child):
    """Returns (client, conv_store, audit, minor_child) for admin route tests."""
    conn = _make_conn()
    user_store = _make_user_store(
        conn,
        (admin_user, "adminpass"),
        (minor_child, None),
    )
    # Wire get_parent so _is_minor_child returns True for minor_child
    parent_user = User(id=99, name="Parent", role="member", username="parent")
    user_store.get_parent.side_effect = lambda uid: parent_user if uid == minor_child.id else None

    session_store = SqliteWebSessionStore(ttl_days=7)
    session_store._conn = conn
    conv_store = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
    conv_store._conn = conn
    audit = MagicMock()
    elevation_store = _make_elevation_store()

    client = _build_client(conn, user_store, session_store, conv_store, elevation_store, audit)

    # Log in as admin
    r = client.post("/login", data={"username": "admin", "password": "adminpass"})
    assert r.status_code == 303
    client.cookies.set("web_session", r.cookies["web_session"])

    return client, conv_store, audit, minor_child


# ── GET /api/conversations ─────────────────────────────────────────────────────

class TestApiListConversations:
    def test_unauthenticated_returns_401(self, app_client):
        r = app_client.get("/api/conversations")
        assert r.status_code == 401

    def test_authenticated_returns_list(self, authed_client):
        r = authed_client.get("/api/conversations")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ── GET /api/conversations/search ─────────────────────────────────────────────

class TestApiSearchConversations:
    def test_unauthenticated_returns_401(self, app_client):
        r = app_client.get("/api/conversations/search?q=hello")
        assert r.status_code == 401

    def test_empty_query_returns_empty_list(self, authed_client):
        r = authed_client.get("/api/conversations/search?q=")
        assert r.status_code == 200
        assert r.json() == []


# ── GET /api/conversations/{id}/messages ──────────────────────────────────────

class TestApiGetMessages:
    def test_unauthenticated_returns_401(self, app_client):
        r = app_client.get("/api/conversations/1/messages")
        assert r.status_code == 401

    def test_own_conversation_returns_200(self, authed_client, alice):
        import web_router as wr
        cid = wr._conv_store.create(alice.id)
        r = authed_client.get(f"/api/conversations/{cid}/messages")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_other_users_conversation_returns_404(self, authed_client):
        import web_router as wr
        # Create a conv for a different user
        cid = wr._conv_store.create(user_id=999)
        r = authed_client.get(f"/api/conversations/{cid}/messages")
        assert r.status_code == 404


# ── PATCH /api/conversations/{id} ─────────────────────────────────────────────

class TestApiRenameConversation:
    def test_rename_success(self, authed_client, alice):
        import web_router as wr
        cid = wr._conv_store.create(alice.id)
        r = authed_client.patch(f"/api/conversations/{cid}", json={"title": "New Name"})
        assert r.status_code == 200
        assert r.json()["title"] == "New Name"

    def test_rename_empty_title_returns_400(self, authed_client, alice):
        import web_router as wr
        cid = wr._conv_store.create(alice.id)
        r = authed_client.patch(f"/api/conversations/{cid}", json={"title": "  "})
        assert r.status_code == 400

    def test_rename_other_users_conv_returns_404(self, authed_client):
        import web_router as wr
        cid = wr._conv_store.create(user_id=999)
        r = authed_client.patch(f"/api/conversations/{cid}", json={"title": "Steal"})
        assert r.status_code == 404

    def test_rename_writes_audit_log(self, authed_client, alice):
        import web_router as wr
        cid = wr._conv_store.create(alice.id)
        authed_client.patch(f"/api/conversations/{cid}", json={"title": "Audited"})
        wr._audit.log.assert_called()


# ── GET /admin/users ──────────────────────────────────────────────────────────

class TestAdminUsers:
    def test_non_admin_returns_403(self, authed_client):
        r = authed_client.get("/admin/users")
        assert r.status_code == 403

    def test_unauthenticated_redirects_to_login(self, app_client):
        r = app_client.get("/admin/users")
        assert r.status_code == 303
        assert "/login" in r.headers["location"]

    def test_admin_gets_200_with_user_list(self, admin_ctx):
        client, *_ = admin_ctx
        r = client.get("/admin/users")
        assert r.status_code == 200
        assert "Child" in r.text


# ── GET /admin/users/{id}/conversations ───────────────────────────────────────

class TestAdminUserConversations:
    def test_non_minor_child_returns_403(self, admin_ctx, admin_user):
        client, conv_store, _, minor_child = admin_ctx
        # admin_user.id is not a minor child (no parent_link)
        r = client.get(f"/admin/users/{admin_user.id}/conversations")
        assert r.status_code == 403

    def test_minor_child_returns_200(self, admin_ctx, minor_child):
        client, conv_store, _, _ = admin_ctx
        conv_store.create(user_id=minor_child.id)
        r = client.get(f"/admin/users/{minor_child.id}/conversations")
        assert r.status_code == 200

    def test_unknown_user_returns_404(self, admin_ctx):
        client, *_ = admin_ctx
        r = client.get("/admin/users/99999/conversations")
        assert r.status_code == 404


# ── GET /admin/conversations/{id} ─────────────────────────────────────────────

class TestAdminConversationView:
    def test_minor_child_conv_returns_200(self, admin_ctx, minor_child):
        client, conv_store, _, _ = admin_ctx
        cid = conv_store.create(user_id=minor_child.id)
        r = client.get(f"/admin/conversations/{cid}")
        assert r.status_code == 200

    def test_stealth_read_audit_is_logged(self, admin_ctx, minor_child):
        client, conv_store, audit, _ = admin_ctx
        cid = conv_store.create(user_id=minor_child.id)
        client.get(f"/admin/conversations/{cid}")
        calls = [str(c) for c in audit.log.call_args_list]
        assert any("stealth_read_web_conversation" in c for c in calls)

    def test_non_minor_conv_returns_403(self, admin_ctx, admin_user):
        client, conv_store, _, _ = admin_ctx
        # admin_user has no parent → not a minor child
        cid = conv_store.create(user_id=admin_user.id)
        r = client.get(f"/admin/conversations/{cid}")
        assert r.status_code == 403

    def test_unknown_conv_returns_404(self, admin_ctx):
        client, *_ = admin_ctx
        r = client.get("/admin/conversations/99999")
        assert r.status_code == 404
