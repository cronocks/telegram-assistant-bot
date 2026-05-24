"""tests/test_task_web.py — RED tests for sub-task 7.7 Web API / UI for tasks.

Uses FastAPI TestClient + MagicMock task_store.
Routes expected:
  GET    /api/tasks                  — list pending tasks (JSON)
  POST   /api/tasks                  — create task (JSON)
  PATCH  /api/tasks/{id}/complete    — mark complete (JSON)
  DELETE /api/tasks/{id}             — cancel task (JSON)
  GET    /tasks                      — task list HTML page
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from interfaces import User
from web_channel import WebChannelAdapter
from web_session_store import SqliteWebSessionStore
from web_router import router, init_web_router

# ── DB / session helpers ──────────────────────────────────────────────────────

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT,
            role TEXT NOT NULL DEFAULT 'member',
            birthdate DATE,
            password_hash TEXT,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            daily_summary_time TEXT,
            morning_default_time TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            deleted_at DATETIME
        );
        CREATE TABLE web_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL,
            revoked_at DATETIME
        );
        CREATE INDEX idx_web_sessions_token ON web_sessions(token);
        CREATE TABLE sudo_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            locked_until DATETIME,
            last_attempt_at DATETIME
        );
    """)
    return conn


def _insert_session(conn: sqlite3.Connection, user_id: int, token: str) -> None:
    conn.execute(
        """INSERT INTO web_sessions (user_id, token, created_at, expires_at)
           VALUES (?, ?, datetime('now'), datetime('now', '+7 days'))""",
        (user_id, token),
    )
    conn.commit()


def _build_client(user: User, task_store=None) -> tuple[TestClient, sqlite3.Connection]:
    """Return (TestClient, conn) — conn is needed to insert sessions."""
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

    init_web_router(
        templates=templates,
        web_channel=web_ch,
        session_store=session_store,
        user_store=user_store,
        audit=audit,
        elevation_store=elevation_store,
        conv_store=conv_store,
        task_store=task_store,
    )

    app = FastAPI()
    app.include_router(router)
    app.state.web_deps = MagicMock()

    client = TestClient(app, follow_redirects=False)
    return client, conn


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def alice() -> User:
    return User(id=1, name="Alice", role="member", username="alice")


@pytest.fixture()
def fake_task():
    return {
        "id": 42,
        "user_id": 1,
        "title": "Bài tập toán",
        "deadline": "2026-06-10T09:00:00+07:00",
        "status": "pending",
        "created_at": "2026-06-07T08:00:00+07:00",
        "completed_at": None,
    }


@pytest.fixture()
def task_store(fake_task):
    store = MagicMock()
    store.list_pending_due.return_value = [fake_task]
    store.list_completed_on.return_value = []
    store.get_task.return_value = fake_task
    store.create_task.return_value = fake_task
    store.complete_task.return_value = {**fake_task, "status": "completed"}
    store.cancel_task.return_value = {**fake_task, "status": "cancelled"}
    return store


# ── GET /api/tasks ─────────────────────────────────────────────────────────────


class TestApiListTasks:
    def test_requires_auth(self, alice, task_store):
        client, _ = _build_client(alice, task_store)
        r = client.get("/api/tasks")
        assert r.status_code == 401

    def test_returns_pending_tasks(self, alice, task_store, fake_task):
        client, conn = _build_client(alice, task_store)
        _insert_session(conn, alice.id, "tok1")
        r = client.get("/api/tasks", cookies={"web_session": "tok1"})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert any(t["id"] == fake_task["id"] for t in data)

    def test_date_filter_passes_correct_deadline(self, alice, task_store):
        client, conn = _build_client(alice, task_store)
        _insert_session(conn, alice.id, "tok2")
        client.get("/api/tasks?date=2026-06-10", cookies={"web_session": "tok2"})
        # list_pending_due should have been called with a before_iso containing 2026-06-10
        call_args = task_store.list_pending_due.call_args
        before_iso = call_args[0][0] if call_args[0] else call_args[1].get("before_iso", "")
        assert "2026-06-10" in before_iso


# ── POST /api/tasks ────────────────────────────────────────────────────────────


class TestApiCreateTask:
    def test_requires_auth(self, alice, task_store):
        client, _ = _build_client(alice, task_store)
        r = client.post("/api/tasks", json={"title": "Task", "deadline": "2026-06-10T09:00:00+07:00"})
        assert r.status_code == 401

    def test_creates_and_returns_201(self, alice, task_store, fake_task):
        client, conn = _build_client(alice, task_store)
        _insert_session(conn, alice.id, "tok3")
        r = client.post(
            "/api/tasks",
            json={"title": "Bài tập toán", "deadline": "2026-06-10T09:00:00+07:00"},
            cookies={"web_session": "tok3"},
        )
        assert r.status_code == 201
        assert r.json()["id"] == fake_task["id"]
        task_store.create_task.assert_called_once()

    def test_missing_title_returns_422(self, alice, task_store):
        client, conn = _build_client(alice, task_store)
        _insert_session(conn, alice.id, "tok4")
        r = client.post(
            "/api/tasks",
            json={"deadline": "2026-06-10T09:00:00+07:00"},
            cookies={"web_session": "tok4"},
        )
        assert r.status_code == 422


# ── PATCH /api/tasks/{id}/complete ────────────────────────────────────────────


class TestApiCompleteTask:
    def test_requires_auth(self, alice, task_store):
        client, _ = _build_client(alice, task_store)
        r = client.patch("/api/tasks/42/complete")
        assert r.status_code == 401

    def test_marks_complete(self, alice, task_store):
        client, conn = _build_client(alice, task_store)
        _insert_session(conn, alice.id, "tok5")
        r = client.patch("/api/tasks/42/complete", cookies={"web_session": "tok5"})
        assert r.status_code == 200
        task_store.complete_task.assert_called_once_with(42)

    def test_not_found_returns_404(self, alice, task_store):
        task_store.complete_task.return_value = None
        task_store.get_task.return_value = None
        client, conn = _build_client(alice, task_store)
        _insert_session(conn, alice.id, "tok6")
        r = client.patch("/api/tasks/999/complete", cookies={"web_session": "tok6"})
        assert r.status_code == 404


# ── DELETE /api/tasks/{id} ────────────────────────────────────────────────────


class TestApiCancelTask:
    def test_requires_auth(self, alice, task_store):
        client, _ = _build_client(alice, task_store)
        r = client.delete("/api/tasks/42")
        assert r.status_code == 401

    def test_cancels_task(self, alice, task_store):
        client, conn = _build_client(alice, task_store)
        _insert_session(conn, alice.id, "tok7")
        r = client.delete("/api/tasks/42", cookies={"web_session": "tok7"})
        assert r.status_code == 200
        task_store.cancel_task.assert_called_once_with(42)

    def test_not_found_returns_404(self, alice, task_store):
        task_store.cancel_task.return_value = None
        task_store.get_task.return_value = None
        client, conn = _build_client(alice, task_store)
        _insert_session(conn, alice.id, "tok8")
        r = client.delete("/api/tasks/999", cookies={"web_session": "tok8"})
        assert r.status_code == 404


# ── GET /tasks ────────────────────────────────────────────────────────────────


class TestTasksPage:
    def test_requires_auth(self, alice, task_store):
        client, _ = _build_client(alice, task_store)
        r = client.get("/tasks")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    def test_renders_html(self, alice, task_store):
        client, conn = _build_client(alice, task_store)
        _insert_session(conn, alice.id, "tok9")
        r = client.get("/tasks", cookies={"web_session": "tok9"})
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
