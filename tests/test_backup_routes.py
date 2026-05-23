"""tests/test_backup_routes.py — Integration tests for backup/import routes (FR-6).

Covers:
  - GET /settings/export (self-export)
  - GET /admin/users/{id}/export (admin export)
  - GET /admin/import (form page)
  - POST /admin/import/preview
  - POST /admin/import/apply
  - _zip_response / _export_filename helpers
  - _store_import_token / _consume_import_token
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from backup_engine import (
    FORMAT_VERSION,
    BackupEngine,
    ExportError,
    ImportFormatError,
    ImportResult,
    ParsedImport,
)
from interfaces import User
from web_channel import WebChannelAdapter
from web_conversation_store import SqliteWebConversationStore
from web_router import (
    _consume_import_token,
    _export_filename,
    _import_tokens,
    _store_import_token,
    _zip_response,
    init_web_router,
    router,
)
from web_session_store import SqliteWebSessionStore


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
    from auth import hash_password
    user_map = {}
    for user, password in users_with_pw:
        pw_hash = hash_password(password) if password else None
        conn.execute(
            "INSERT INTO users (id, name, username, role, birthdate, password_hash) VALUES (?,?,?,?,?,?)",
            (user.id, user.name, user.username, user.role,
             str(user.birthdate) if user.birthdate else None, pw_hash),
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
        __import__("auth").verify_password(plain, user_map[uid][1])
        if uid in user_map and user_map[uid][1] else False
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


def _make_zip_bytes(user_name: str = "ExportUser") -> bytes:
    manifest = {
        "format_version": FORMAT_VERSION,
        "exported_at": "2025-01-01T00:00:00+00:00",
        "exporter": "test",
        "source_user": {"id": 1, "name": user_name, "role": "member"},
        "stats": {},
    }
    data = {
        "user": {"id": 1, "name": user_name, "role": "member"},
        "channel_bindings": [],
        "quota": None,
        "parent_links_as_child": [],
        "notes": [],
        "wiki_pages": [],
        "user_memory": [],
        "web_conversations": [],
        "audit_entries": [],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("data.json", json.dumps(data))
    return buf.getvalue()


def _build_client(conn, user_store, session_store, conv_store, elevation_store, audit,
                  backup_engine=None):
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
        backup_engine=backup_engine,
    )

    app = FastAPI()
    app.include_router(router)
    app.state.web_deps = MagicMock()
    return TestClient(app, follow_redirects=False)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def alice():
    return User(id=1, name="Alice", role="member", username="alice")


@pytest.fixture()
def admin_user():
    return User(id=10, name="Admin", role="admin", username="admin")


@pytest.fixture()
def member_target():
    return User(id=5, name="TargetUser", role="member", username="targetuser")


@pytest.fixture()
def mock_engine():
    engine = MagicMock(spec=BackupEngine)
    engine.export_cooldown_remaining.return_value = 0
    engine.generate_export.return_value = (_make_zip_bytes(), {"stats": {}})
    return engine


@pytest.fixture()
def admin_client(admin_user):
    conn = _make_conn()
    user_store = _make_user_store(conn, (admin_user, "adminpass"))
    session_store = SqliteWebSessionStore(ttl_days=7)
    session_store._conn = conn
    conv_store = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
    conv_store._conn = conn
    audit = MagicMock()
    elevation_store = _make_elevation_store()
    return _build_client(conn, user_store, session_store, conv_store, elevation_store, audit)


@pytest.fixture()
def admin_authed(admin_client):
    r = admin_client.post("/login", data={"username": "admin", "password": "adminpass"})
    assert r.status_code == 303
    admin_client.cookies.set("web_session", r.cookies["web_session"])
    return admin_client


@pytest.fixture()
def member_client(alice):
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
def member_authed(member_client):
    r = member_client.post("/login", data={"username": "alice", "password": "secret123"})
    assert r.status_code == 303
    member_client.cookies.set("web_session", r.cookies["web_session"])
    return member_client


# ── _zip_response / _export_filename ─────────────────────────────────────────

class TestHelpers:
    def test_zip_response_has_correct_content_type(self):
        resp = _zip_response(b"PK...", "export.zip")
        assert resp.media_type == "application/zip"

    def test_zip_response_has_content_disposition(self):
        resp = _zip_response(b"PK...", "my_export.zip")
        assert "my_export.zip" in resp.headers["content-disposition"]

    def test_export_filename_contains_user_name(self):
        name = _export_filename("Alice")
        assert "Alice" in name
        assert name.endswith(".zip")

    def test_export_filename_sanitizes_special_chars(self):
        name = _export_filename("User@Name!")
        assert "@" not in name
        assert "!" not in name


# ── Token helpers ─────────────────────────────────────────────────────────────

class TestImportTokens:
    def setup_method(self):
        _import_tokens.clear()

    def test_store_and_consume(self):
        parsed = ParsedImport(
            manifest={"format_version": 1},
            data={},
            notes_content={},
            wiki_content={},
        )
        token = _store_import_token(parsed)
        result = _consume_import_token(token)
        assert result is parsed

    def test_consume_returns_none_for_missing_token(self):
        assert _consume_import_token("invalid-token") is None

    def test_consume_removes_token(self):
        parsed = ParsedImport(manifest={}, data={}, notes_content={}, wiki_content={})
        token = _store_import_token(parsed)
        _consume_import_token(token)
        assert _consume_import_token(token) is None

    def test_expired_token_returns_none(self):
        from datetime import timedelta
        parsed = ParsedImport(manifest={}, data={}, notes_content={}, wiki_content={})
        token = _store_import_token(parsed)
        # Manually expire it.
        _import_tokens[token]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert _consume_import_token(token) is None


# ── GET /settings/export ─────────────────────────────────────────────────────

class TestSelfExport:
    def test_unauthenticated_redirects_to_login(self, member_client):
        r = member_client.get("/settings/export")
        assert r.status_code == 303
        assert "/login" in r.headers["location"]

    def test_returns_503_when_engine_not_configured(self, member_authed):
        r = member_authed.get("/settings/export")
        assert r.status_code == 503

    def test_returns_429_on_cooldown(self, alice):
        conn = _make_conn()
        user_store = _make_user_store(conn, (alice, "secret123"))
        session_store = SqliteWebSessionStore(ttl_days=7)
        session_store._conn = conn
        conv_store = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
        conv_store._conn = conn
        audit = MagicMock()
        elevation_store = _make_elevation_store()
        engine = MagicMock(spec=BackupEngine)
        engine.export_cooldown_remaining.return_value = 120

        client = _build_client(conn, user_store, session_store, conv_store,
                               elevation_store, audit, backup_engine=engine)
        r = client.post("/login", data={"username": "alice", "password": "secret123"})
        client.cookies.set("web_session", r.cookies["web_session"])

        r = client.get("/settings/export")
        assert r.status_code == 429

    def test_returns_zip_on_success(self, alice):
        conn = _make_conn()
        user_store = _make_user_store(conn, (alice, "secret123"))
        session_store = SqliteWebSessionStore(ttl_days=7)
        session_store._conn = conn
        conv_store = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
        conv_store._conn = conn
        audit = MagicMock()
        elevation_store = _make_elevation_store()
        engine = MagicMock(spec=BackupEngine)
        engine.export_cooldown_remaining.return_value = 0
        engine.generate_export.return_value = (_make_zip_bytes(), {"stats": {}})

        client = _build_client(conn, user_store, session_store, conv_store,
                               elevation_store, audit, backup_engine=engine)
        r = client.post("/login", data={"username": "alice", "password": "secret123"})
        client.cookies.set("web_session", r.cookies["web_session"])

        r = client.get("/settings/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/zip")


# ── GET /admin/users/{id}/export ─────────────────────────────────────────────

class TestAdminExport:
    def _make_admin_ctx(self, admin_user, target_user, engine):
        conn = _make_conn()
        user_store = _make_user_store(conn, (admin_user, "adminpass"), (target_user, None))
        session_store = SqliteWebSessionStore(ttl_days=7)
        session_store._conn = conn
        conv_store = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
        conv_store._conn = conn
        audit = MagicMock()
        elevation_store = _make_elevation_store()
        client = _build_client(conn, user_store, session_store, conv_store,
                               elevation_store, audit, backup_engine=engine)
        r = client.post("/login", data={"username": "admin", "password": "adminpass"})
        client.cookies.set("web_session", r.cookies["web_session"])
        return client

    def test_member_gets_403(self, member_authed, member_target):
        r = member_authed.get(f"/admin/users/{member_target.id}/export")
        assert r.status_code == 403

    def test_unknown_target_returns_404(self, admin_user):
        engine = MagicMock(spec=BackupEngine)
        engine.export_cooldown_remaining.return_value = 0
        # Build a client where get_user_by_id always returns None for non-admin IDs.
        conn = _make_conn()
        user_store = _make_user_store(conn, (admin_user, "adminpass"))
        user_store.get_user_by_id.side_effect = lambda uid: admin_user if uid == admin_user.id else None
        session_store = SqliteWebSessionStore(ttl_days=7)
        session_store._conn = conn
        conv_store = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
        conv_store._conn = conn
        audit = MagicMock()
        elevation_store = _make_elevation_store()
        client = _build_client(conn, user_store, session_store, conv_store,
                               elevation_store, audit, backup_engine=engine)
        r = client.post("/login", data={"username": "admin", "password": "adminpass"})
        client.cookies.set("web_session", r.cookies["web_session"])
        r = client.get("/admin/users/999/export")
        assert r.status_code == 404

    def test_returns_zip_for_valid_target(self, admin_user, member_target):
        engine = MagicMock(spec=BackupEngine)
        engine.export_cooldown_remaining.return_value = 0
        engine.generate_export.return_value = (_make_zip_bytes("TargetUser"), {})
        client = self._make_admin_ctx(admin_user, member_target, engine)
        r = client.get(f"/admin/users/{member_target.id}/export")
        assert r.status_code == 200
        assert "zip" in r.headers["content-type"]


# ── GET /admin/import ─────────────────────────────────────────────────────────

class TestAdminImportPage:
    def test_member_gets_403(self, member_authed):
        r = member_authed.get("/admin/import")
        assert r.status_code == 403

    def test_admin_sees_upload_form(self, admin_authed):
        r = admin_authed.get("/admin/import")
        assert r.status_code == 200
        assert b"upload" in r.content.lower() or b"import" in r.content.lower()


# ── POST /admin/import/preview ────────────────────────────────────────────────

class TestAdminImportPreview:
    def _make_client_with_engine(self, admin_user, engine):
        conn = _make_conn()
        user_store = _make_user_store(conn, (admin_user, "adminpass"))
        session_store = SqliteWebSessionStore(ttl_days=7)
        session_store._conn = conn
        conv_store = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
        conv_store._conn = conn
        audit = MagicMock()
        elevation_store = _make_elevation_store()
        client = _build_client(conn, user_store, session_store, conv_store,
                               elevation_store, audit, backup_engine=engine)
        r = client.post("/login", data={"username": "admin", "password": "adminpass"})
        client.cookies.set("web_session", r.cookies["web_session"])
        return client

    def test_member_gets_403(self, member_authed):
        r = member_authed.post("/admin/import/preview",
                               files={"zip_file": ("x.zip", b"PK", "application/zip")})
        assert r.status_code == 403

    def test_invalid_zip_returns_400(self, admin_user):
        engine = MagicMock(spec=BackupEngine)
        engine.parse_import.side_effect = ImportFormatError("bad zip")
        client = self._make_client_with_engine(admin_user, engine)
        r = client.post("/admin/import/preview",
                        files={"zip_file": ("x.zip", b"notazip", "application/zip")})
        assert r.status_code == 400

    def test_valid_zip_shows_preview(self, admin_user):
        parsed = ParsedImport(
            manifest={
                "format_version": FORMAT_VERSION,
                "exported_at": "2025-01-01T00:00:00+00:00",
                "source_user": {"name": "ExportUser", "username": None, "role": "member"},
                "stats": {
                    "notes": 1, "wiki_pages": 0,
                    "web_conversations": 0, "web_messages": 0,
                    "memory_kinds": 0, "size_bytes_uncompressed": 1024,
                },
            },
            data={},
            notes_content={},
            wiki_content={},
            warnings=[],
        )
        engine = MagicMock(spec=BackupEngine)
        engine.parse_import.return_value = parsed
        client = self._make_client_with_engine(admin_user, engine)
        r = client.post("/admin/import/preview",
                        files={"zip_file": ("export.zip", _make_zip_bytes(), "application/zip")})
        assert r.status_code == 200
        assert b"ExportUser" in r.content


# ── POST /admin/import/apply ──────────────────────────────────────────────────

class TestAdminImportApply:
    def _make_client_with_engine(self, admin_user, engine):
        conn = _make_conn()
        user_store = _make_user_store(conn, (admin_user, "adminpass"))
        session_store = SqliteWebSessionStore(ttl_days=7)
        session_store._conn = conn
        conv_store = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
        conv_store._conn = conn
        audit = MagicMock()
        elevation_store = _make_elevation_store()
        client = _build_client(conn, user_store, session_store, conv_store,
                               elevation_store, audit, backup_engine=engine)
        r = client.post("/login", data={"username": "admin", "password": "adminpass"})
        client.cookies.set("web_session", r.cookies["web_session"])
        return client

    def test_invalid_token_returns_400(self, admin_user):
        engine = MagicMock(spec=BackupEngine)
        client = self._make_client_with_engine(admin_user, engine)
        r = client.post("/admin/import/apply", data={"token": "bad-token"})
        assert r.status_code == 400

    def test_valid_token_shows_result(self, admin_user):
        parsed = ParsedImport(
            manifest={"source_user": {"name": "X"}},
            data={},
            notes_content={},
            wiki_content={},
        )
        import_result = ImportResult(
            new_user_id=99,
            counts={"notes": 0, "wiki_pages": 0, "web_conversations": 0, "web_messages": 0},
            id_map={},
            warnings=[],
        )
        engine = MagicMock(spec=BackupEngine)
        engine.apply_import.return_value = import_result
        client = self._make_client_with_engine(admin_user, engine)

        # Store a token manually and POST it.
        _import_tokens.clear()
        token = _store_import_token(parsed)
        r = client.post("/admin/import/apply", data={"token": token})
        assert r.status_code == 200
        assert b"result" in r.content.lower() or b"99" in r.content

    def test_apply_failure_shows_error(self, admin_user):
        parsed = ParsedImport(
            manifest={"source_user": {"name": "X"}},
            data={},
            notes_content={},
            wiki_content={},
        )
        engine = MagicMock(spec=BackupEngine)
        engine.apply_import.side_effect = RuntimeError("DB error")
        client = self._make_client_with_engine(admin_user, engine)

        _import_tokens.clear()
        token = _store_import_token(parsed)
        r = client.post("/admin/import/apply", data={"token": token})
        assert r.status_code == 500
