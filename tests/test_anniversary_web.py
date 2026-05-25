"""Tests for FR-8 anniversary web routes."""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from anniversary_store import SqliteAnniversaryStore
from interfaces import User
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


def _build_client(user: User) -> tuple[TestClient, sqlite3.Connection, SqliteAnniversaryStore, MagicMock]:
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

    anniv_store = SqliteAnniversaryStore(conn=conn)
    anniv_engine = MagicMock()
    anniv_engine.compute_year.return_value = 0
    anniv_engine.cancel_all_for_anniversary.return_value = 0

    init_web_router(
        templates=templates,
        web_channel=web_ch,
        session_store=session_store,
        user_store=user_store,
        audit=audit,
        elevation_store=elevation_store,
        conv_store=conv_store,
        task_store=None,
        anniversary_store=anniv_store,
        anniversary_engine=anniv_engine,
    )

    app = FastAPI()
    app.include_router(router)
    app.state.web_deps = MagicMock()
    return TestClient(app, follow_redirects=False), conn, anniv_store, anniv_engine


@pytest.fixture()
def alice() -> User:
    return User(id=1, name="Alice", role="member", username="alice")


# ── GET /anniversaries ────────────────────────────────────────────────────────


def test_list_requires_auth(alice):
    client, _, _, _ = _build_client(alice)
    r = client.get("/anniversaries")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")


def test_list_renders_empty(alice):
    client, conn, _, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t1")
    r = client.get("/anniversaries", cookies={"web_session": "t1"})
    assert r.status_code == 200
    assert "chưa có" in r.text.lower() or "kỷ niệm" in r.text.lower()


def test_list_renders_user_rows(alice):
    client, conn, anniv_store, _ = _build_client(alice)
    anniv_store.create_anniversary(
        user_id=alice.id, name="Giỗ ông", date_type="lunar", month=3, day=10,
    )
    _insert_session(conn, alice.id, "t2")
    r = client.get("/anniversaries", cookies={"web_session": "t2"})
    assert r.status_code == 200
    assert "Giỗ ông" in r.text


def test_list_excludes_other_users(alice):
    client, conn, anniv_store, _ = _build_client(alice)
    conn.execute("INSERT INTO users (id, name, role) VALUES (999, 'Other', 'member')")
    conn.commit()
    anniv_store.create_anniversary(
        user_id=999, name="Other user secret", date_type="lunar", month=1, day=1,
    )
    _insert_session(conn, alice.id, "t3")
    r = client.get("/anniversaries", cookies={"web_session": "t3"})
    assert "Other user secret" not in r.text


# ── GET /anniversaries/new ────────────────────────────────────────────────────


def test_new_form_renders(alice):
    client, conn, _, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t4")
    r = client.get("/anniversaries/new", cookies={"web_session": "t4"})
    assert r.status_code == 200
    assert "<form" in r.text


# ── POST /anniversaries ───────────────────────────────────────────────────────


def test_create_redirects_to_detail(alice):
    client, conn, anniv_store, anniv_engine = _build_client(alice)
    _insert_session(conn, alice.id, "t5")
    r = client.post(
        "/anniversaries",
        data={
            "name": "Test", "date_type": "lunar",
            "day": "10", "month": "3", "category": "gio",
            "reminder_offsets": "30,7,0",
        },
        cookies={"web_session": "t5"},
    )
    assert r.status_code in (302, 303)
    # Should have created row.
    rows = anniv_store.list_for_user(alice.id)
    assert len(rows) == 1
    assert rows[0]["name"] == "Test"
    # Should have triggered compute_year.
    assert anniv_engine.compute_year.called


def test_create_validation_error_shows_form(alice):
    client, conn, anniv_store, _ = _build_client(alice)
    _insert_session(conn, alice.id, "t6")
    r = client.post(
        "/anniversaries",
        data={
            "name": "", "date_type": "lunar",
            "day": "10", "month": "3", "category": "gio",
        },
        cookies={"web_session": "t6"},
    )
    # Either 400 or rerender form — anyway no row created.
    assert len(anniv_store.list_for_user(alice.id)) == 0


# ── GET /anniversaries/{id} ───────────────────────────────────────────────────


def test_view_owner(alice):
    client, conn, anniv_store, _ = _build_client(alice)
    a = anniv_store.create_anniversary(
        user_id=alice.id, name="View me", date_type="solar", month=8, day=15,
    )
    _insert_session(conn, alice.id, "t7")
    r = client.get(f"/anniversaries/{a['id']}", cookies={"web_session": "t7"})
    assert r.status_code == 200
    assert "View me" in r.text


def test_view_not_owner_returns_404(alice):
    client, conn, anniv_store, _ = _build_client(alice)
    conn.execute("INSERT INTO users (id, name, role) VALUES (999, 'Other', 'member')")
    conn.commit()
    a = anniv_store.create_anniversary(
        user_id=999, name="Not yours", date_type="solar", month=8, day=15,
    )
    _insert_session(conn, alice.id, "t8")
    r = client.get(f"/anniversaries/{a['id']}", cookies={"web_session": "t8"})
    assert r.status_code == 404


# ── GET /anniversaries/{id}/edit ──────────────────────────────────────────────


def test_edit_form_renders(alice):
    client, conn, anniv_store, _ = _build_client(alice)
    a = anniv_store.create_anniversary(
        user_id=alice.id, name="Edit me", date_type="lunar", month=1, day=1,
    )
    _insert_session(conn, alice.id, "t9")
    r = client.get(f"/anniversaries/{a['id']}/edit", cookies={"web_session": "t9"})
    assert r.status_code == 200
    assert "Edit me" in r.text


# ── POST /anniversaries/{id} (update) ─────────────────────────────────────────


def test_update_persists_changes(alice):
    client, conn, anniv_store, anniv_engine = _build_client(alice)
    a = anniv_store.create_anniversary(
        user_id=alice.id, name="Old", date_type="lunar", month=1, day=1,
    )
    _insert_session(conn, alice.id, "t10")
    r = client.post(
        f"/anniversaries/{a['id']}",
        data={
            "name": "New name", "date_type": "solar",
            "day": "15", "month": "8", "category": "cuoi",
            "reminder_offsets": "7,3,0", "enabled": "1",
        },
        cookies={"web_session": "t10"},
    )
    assert r.status_code in (302, 303)
    row = anniv_store.get_anniversary(a["id"])
    assert row["name"] == "New name"
    assert row["date_type"] == "solar"
    assert row["day"] == 15
    assert row["month"] == 8
    # Re-compute should have been triggered on date change.
    assert anniv_engine.compute_year.called


def test_update_not_owner_returns_404(alice):
    client, conn, anniv_store, _ = _build_client(alice)
    conn.execute("INSERT INTO users (id, name, role) VALUES (999, 'Other', 'member')")
    conn.commit()
    a = anniv_store.create_anniversary(
        user_id=999, name="X", date_type="lunar", month=1, day=1,
    )
    _insert_session(conn, alice.id, "t11")
    r = client.post(
        f"/anniversaries/{a['id']}",
        data={
            "name": "Hacked", "date_type": "lunar",
            "day": "1", "month": "1", "category": "khac",
            "reminder_offsets": "0",
        },
        cookies={"web_session": "t11"},
    )
    assert r.status_code == 404


# ── POST /anniversaries/{id}/delete ───────────────────────────────────────────


def test_delete_soft_deletes(alice):
    client, conn, anniv_store, anniv_engine = _build_client(alice)
    a = anniv_store.create_anniversary(
        user_id=alice.id, name="Trash", date_type="lunar", month=1, day=1,
    )
    _insert_session(conn, alice.id, "t12")
    r = client.post(
        f"/anniversaries/{a['id']}/delete",
        cookies={"web_session": "t12"},
    )
    assert r.status_code in (302, 303)
    row = anniv_store.get_anniversary(a["id"])
    assert row["deleted_at"] is not None
    # Pending reminders should have been cancelled.
    assert anniv_engine.cancel_all_for_anniversary.called


def test_delete_not_owner_returns_404(alice):
    client, conn, anniv_store, _ = _build_client(alice)
    conn.execute("INSERT INTO users (id, name, role) VALUES (999, 'Other', 'member')")
    conn.commit()
    a = anniv_store.create_anniversary(
        user_id=999, name="X", date_type="lunar", month=1, day=1,
    )
    _insert_session(conn, alice.id, "t13")
    r = client.post(
        f"/anniversaries/{a['id']}/delete",
        cookies={"web_session": "t13"},
    )
    assert r.status_code == 404
    row = anniv_store.get_anniversary(a["id"])
    assert row["deleted_at"] is None
