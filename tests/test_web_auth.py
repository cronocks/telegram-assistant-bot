"""tests/test_web_auth.py — Integration tests for web auth routes (FR-5).

Uses FastAPI TestClient to exercise login/logout/setup-password flows.
Stubs out adapters with lightweight in-memory fakes.
"""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates

from interfaces import User, ChannelMessage
from web_channel import WebChannelAdapter
from web_session_store import SqliteWebSessionStore
from web_router import router, init_web_router


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_db():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE users (
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
        CREATE TABLE web_sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            token      TEXT NOT NULL UNIQUE,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL,
            revoked_at DATETIME
        );
        CREATE INDEX idx_web_sessions_token ON web_sessions(token);
    """)
    return c


def _make_user_store(conn, user: User, password: str | None = None):
    """Minimal UserStore stub backed by the in-memory DB."""
    from auth import hash_password

    # Insert user row
    pw_hash = hash_password(password) if password else None
    conn.execute(
        "INSERT INTO users (id, name, username, role, password_hash) VALUES (?,?,?,?,?)",
        (user.id, user.name, user.username, user.role, pw_hash),
    )
    conn.commit()

    store = MagicMock()
    store.find_by_username_or_name.return_value = user
    store.get_user_by_id.return_value = user
    store.get_password_hash.return_value = pw_hash
    store.check_password.side_effect = lambda uid, plain: (
        __import__("auth").verify_password(plain, pw_hash) if pw_hash else False
    )
    store.set_password.side_effect = lambda uid, plain: None
    store.set_must_change_password.side_effect = lambda uid, flag: (
        conn.execute(
            "UPDATE users SET must_change_password = ? WHERE id = ?",
            (1 if flag else 0, uid),
        ) or conn.commit()
    )
    store.get_must_change_password.side_effect = lambda uid: bool(
        (conn.execute("SELECT must_change_password FROM users WHERE id = ?", (uid,)).fetchone() or (0,))[0]
    )
    store._conn = conn
    return store


def _make_elevation_store():
    es = MagicMock()
    es.is_locked.return_value = (False, None)
    es.record_failure.return_value = {"locked": False}
    es.reset_failures.return_value = None
    return es


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def alice():
    return User(id=1, name="Alice", role="admin", username="alice")


@pytest.fixture()
def app_client(alice, tmp_path):
    """TestClient with web router + stubbed dependencies."""
    from fastapi import FastAPI

    conn = _make_db()
    user_store = _make_user_store(conn, alice, password="secret123")
    session_store = SqliteWebSessionStore(ttl_days=7)
    session_store._conn = conn
    web_ch = WebChannelAdapter()
    audit = MagicMock()
    elevation_store = _make_elevation_store()

    # Minimal Jinja2Templates pointing at real templates dir
    import os
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
    templates = Jinja2Templates(directory=templates_dir)

    init_web_router(
        templates=templates,
        web_channel=web_ch,
        session_store=session_store,
        user_store=user_store,
        audit=audit,
        elevation_store=elevation_store,
    )

    fast_app = FastAPI()
    fast_app.include_router(router)
    fast_app.state.web_deps = MagicMock()

    return TestClient(fast_app, follow_redirects=False)


# ── Login ─────────────────────────────────────────────────────────────────────

class TestLogin:
    def test_get_login_page_returns_200(self, app_client):
        r = app_client.get("/login")
        assert r.status_code == 200
        assert "Đăng nhập" in r.text

    def test_login_success_sets_cookie_and_redirects(self, app_client):
        r = app_client.post("/login", data={"username": "alice", "password": "secret123"})
        assert r.status_code == 303
        assert r.headers["location"] == "/chat"
        assert "web_session" in r.cookies

    def test_login_wrong_password_returns_error(self, app_client):
        r = app_client.post("/login", data={"username": "alice", "password": "wrong"})
        assert r.status_code == 400
        assert "không đúng" in r.text

    def test_login_unknown_user_returns_error(self, app_client, alice):
        import web_router as wr
        wr._user_store.find_by_username_or_name.return_value = None
        r = app_client.post("/login", data={"username": "ghost", "password": "x"})
        assert r.status_code == 400

    def test_login_locked_returns_error(self, app_client):
        import web_router as wr
        wr._elevation_store.is_locked.return_value = (True, "10:00")
        r = app_client.post("/login", data={"username": "alice", "password": "secret123"})
        assert r.status_code == 400
        assert "khóa" in r.text
        # Reset for subsequent tests
        wr._elevation_store.is_locked.return_value = (False, None)


# ── Logout ────────────────────────────────────────────────────────────────────

class TestLogout:
    def test_logout_revokes_session_and_clears_cookie(self, app_client):
        # Login first
        r = app_client.post("/login", data={"username": "alice", "password": "secret123"})
        assert "web_session" in r.cookies
        token = r.cookies["web_session"]

        # Logout
        app_client.cookies.set("web_session", token)
        r2 = app_client.post("/logout")
        app_client.cookies.clear()
        assert r2.status_code == 303
        assert r2.headers["location"] == "/login"

        # Token must be revoked — find_active returns None
        import web_router as wr
        assert wr._session_store.find_active(token) is None


# ── Force-reset ───────────────────────────────────────────────────────────────

class TestSetupPassword:
    def test_redirects_to_setup_when_must_change(self, app_client, alice):
        import web_router as wr
        # Simulate must_change_password=1 via the store mock
        wr._user_store.get_must_change_password.side_effect = lambda uid: True
        r = app_client.post("/login", data={"username": "alice", "password": "secret123"})
        assert r.status_code == 303
        assert r.headers["location"] == "/setup-password"
        # Reset
        wr._user_store.get_must_change_password.side_effect = lambda uid: False

    def test_setup_password_mismatch_returns_error(self, app_client):
        r_login = app_client.post("/login", data={"username": "alice", "password": "secret123"})
        app_client.cookies.set("web_session", r_login.cookies.get("web_session"))
        r = app_client.post(
            "/setup-password",
            data={"new_password": "newpass123", "confirm_password": "different"},
        )
        app_client.cookies.clear()
        assert r.status_code == 400
        assert "không khớp" in r.text

    def test_setup_password_too_short_returns_error(self, app_client):
        r_login = app_client.post("/login", data={"username": "alice", "password": "secret123"})
        app_client.cookies.set("web_session", r_login.cookies.get("web_session"))
        r = app_client.post(
            "/setup-password",
            data={"new_password": "short", "confirm_password": "short"},
        )
        app_client.cookies.clear()
        assert r.status_code == 400
        assert "8 ký tự" in r.text

    def test_setup_password_success_redirects_to_chat(self, app_client):
        r_login = app_client.post("/login", data={"username": "alice", "password": "secret123"})
        app_client.cookies.set("web_session", r_login.cookies.get("web_session"))
        r = app_client.post(
            "/setup-password",
            data={"new_password": "newpass123", "confirm_password": "newpass123"},
        )
        app_client.cookies.clear()
        assert r.status_code == 303
        assert r.headers["location"] == "/chat"
        assert "web_session" in r.cookies
