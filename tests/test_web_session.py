"""tests/test_web_session.py — Unit tests for SqliteWebSessionStore (FR-5)."""
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from web_session_store import SqliteWebSessionStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def conn():
    """In-memory SQLite connection with web_sessions schema."""
    c = sqlite3.connect(":memory:")
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
        INSERT INTO users (id, name, role) VALUES (1, 'Alice', 'admin');
        INSERT INTO users (id, name, role) VALUES (2, 'Bob',   'member');

        CREATE TABLE web_sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            token      TEXT NOT NULL UNIQUE,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL,
            revoked_at DATETIME
        );
        CREATE INDEX idx_web_sessions_token ON web_sessions(token);
        CREATE INDEX idx_web_sessions_user  ON web_sessions(user_id);
    """)
    return c


@pytest.fixture()
def store(conn, monkeypatch):
    """SqliteWebSessionStore wired to the in-memory connection."""
    s = SqliteWebSessionStore(ttl_days=7)
    monkeypatch.setattr(s, "_conn", conn)
    return s


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCreate:
    def test_returns_hex_token(self, store):
        token = store.create(1)
        assert isinstance(token, str)
        assert len(token) == 64  # 32 bytes → 64 hex chars

    def test_tokens_are_unique(self, store):
        tokens = {store.create(1) for _ in range(10)}
        assert len(tokens) == 10

    def test_session_row_written(self, store, conn):
        token = store.create(1)
        row = conn.execute("SELECT * FROM web_sessions WHERE token = ?", (token,)).fetchone()
        assert row is not None
        assert row["user_id"] == 1
        assert row["revoked_at"] is None


class TestFindActive:
    def test_find_valid_session(self, store):
        token = store.create(1)
        assert store.find_active(token) == 1

    def test_unknown_token_returns_none(self, store):
        assert store.find_active("nonexistent") is None

    def test_revoked_session_returns_none(self, store):
        token = store.create(1)
        store.revoke(token)
        assert store.find_active(token) is None

    def test_expired_session_returns_none(self, store, conn):
        token = store.create(1)
        # Backdate expires_at to the past
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        conn.execute("UPDATE web_sessions SET expires_at = ? WHERE token = ?", (past, token))
        conn.commit()
        assert store.find_active(token) is None

    def test_different_users_isolated(self, store):
        t1 = store.create(1)
        t2 = store.create(2)
        assert store.find_active(t1) == 1
        assert store.find_active(t2) == 2


class TestRevoke:
    def test_revoke_returns_true_when_found(self, store):
        token = store.create(1)
        assert store.revoke(token) is True

    def test_revoke_unknown_token_returns_false(self, store):
        assert store.revoke("nonexistent") is False

    def test_revoke_twice_returns_false_second_time(self, store):
        token = store.create(1)
        assert store.revoke(token) is True
        assert store.revoke(token) is False

    def test_revoke_sets_revoked_at(self, store, conn):
        token = store.create(1)
        store.revoke(token)
        row = conn.execute("SELECT revoked_at FROM web_sessions WHERE token = ?", (token,)).fetchone()
        assert row["revoked_at"] is not None


class TestRevokeAllForUser:
    def test_revokes_all_user_sessions(self, store):
        t1 = store.create(1)
        t2 = store.create(1)
        t3 = store.create(2)  # different user
        count = store.revoke_all_for_user(1)
        assert count == 2
        assert store.find_active(t1) is None
        assert store.find_active(t2) is None
        assert store.find_active(t3) == 2  # unaffected

    def test_no_active_sessions_returns_zero(self, store):
        assert store.revoke_all_for_user(99) == 0
