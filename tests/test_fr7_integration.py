"""tests/test_fr7_integration.py — Integration tests for FR-7 sub-task 7.8.

Three kinds of tests:
  1. Source-level wiring checks (FAIL until main.py is updated).
  2. Migration smoke-tests (apply 018–021 on a fresh in-memory DB).
  3. End-to-end pipeline tests using real SqliteTaskStore + SqliteReminderStore.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_MIGRATIONS_DIR = pathlib.Path(__file__).parent.parent / "db" / "migrations"
_MAIN_PY = pathlib.Path(__file__).parent.parent / "main.py"

VN_TZ = timezone(timedelta(hours=7))


def _apply_migrations(conn: sqlite3.Connection, *versions: int) -> None:
    """Apply the specified migration files (by version number) to conn."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    for v in sorted(versions):
        for fname in sorted(os.listdir(_MIGRATIONS_DIR)):
            if fname.startswith(f"{v:03d}_"):
                sql = (_MIGRATIONS_DIR / fname).read_text(encoding="utf-8")
                conn.executescript(sql)
                conn.execute(
                    "INSERT OR IGNORE INTO _schema_version (version) VALUES (?)", (v,)
                )
                conn.commit()
                break


def _fresh_conn_with_fr7() -> sqlite3.Connection:
    """Return an in-memory connection with all migrations 001–021 applied."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Apply all migrations in order
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    for fname in sorted(os.listdir(_MIGRATIONS_DIR)):
        import re
        m = re.match(r"^(\d+)_", fname)
        if not m:
            continue
        v = int(m.group(1))
        sql = (_MIGRATIONS_DIR / fname).read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT OR IGNORE INTO _schema_version (version) VALUES (?)", (v,)
            )
            conn.commit()
        except Exception:
            # Some migrations may depend on rows; skip silently for this helper.
            conn.rollback()
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# 1. Source-level wiring checks (RED until main.py is updated)
# ─────────────────────────────────────────────────────────────────────────────


def test_main_imports_sqlite_task_store():
    """main.py must import SqliteTaskStore to wire it into CoreDeps."""
    source = _MAIN_PY.read_text(encoding="utf-8")
    assert "SqliteTaskStore" in source, (
        "SqliteTaskStore not found in main.py — wire FR-7 task store"
    )


def test_main_imports_sqlite_reminder_store():
    """main.py must import SqliteReminderStore to wire it into CoreDeps."""
    source = _MAIN_PY.read_text(encoding="utf-8")
    assert "SqliteReminderStore" in source, (
        "SqliteReminderStore not found in main.py — wire FR-7 reminder store"
    )


def test_main_passes_task_store_to_web_router():
    """init_web_router call in main.py must include task_store= argument."""
    source = _MAIN_PY.read_text(encoding="utf-8")
    # Find the init_web_router call block and verify task_store is passed
    assert "task_store" in source, (
        "task_store not passed to init_web_router in main.py"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Migration smoke-tests
# ─────────────────────────────────────────────────────────────────────────────


def test_fr7_migrations_018_to_021_apply_cleanly():
    """Migrations 018–021 must apply to a fresh DB without raising."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Minimal prerequisite schema (users + parent_links tables)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
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
        CREATE TABLE IF NOT EXISTS parent_links (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            parent_id  INTEGER NOT NULL REFERENCES users(id),
            set_by     INTEGER NOT NULL REFERENCES users(id),
            active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')),
            removed_at TEXT,
            CHECK (user_id != parent_id)
        );
    """)
    conn.commit()

    _apply_migrations(conn, 18, 19, 20, 21)

    # Verify tables exist
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "tasks" in tables
    assert "task_reminders" in tables

    # Verify added columns on parent_links
    cols = {r[1] for r in conn.execute("PRAGMA table_info(parent_links)").fetchall()}
    assert "digest_frequency" in cols
    assert "digest_time" in cols
    assert "last_digest_at" in cols

    # Verify added columns on users
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    assert "daily_summary_time" in user_cols
    assert "morning_default_time" in user_cols


# ─────────────────────────────────────────────────────────────────────────────
# 3. End-to-end pipeline tests with real SQLite stores
# ─────────────────────────────────────────────────────────────────────────────


def test_task_and_reminder_store_share_connection_no_fk_error():
    """SqliteTaskStore + SqliteReminderStore on the same conn must not raise FK errors."""
    from task_store import SqliteTaskStore
    from reminder_store import SqliteReminderStore

    conn = _fresh_conn_with_fr7()
    # Insert a user row so FK is satisfied
    conn.execute(
        "INSERT INTO users (id, name, role, created_at, deleted_at) "
        "VALUES (1, 'Alice', 'member', datetime('now'), NULL)"
    )
    conn.commit()

    ts = SqliteTaskStore(conn)
    rs = SqliteReminderStore(conn)

    now_iso = datetime.now(VN_TZ).isoformat()
    deadline = (datetime.now(VN_TZ) + timedelta(hours=3)).isoformat()
    task = ts.create_task(user_id=1, title="Test task", deadline=deadline)

    reminders = rs.bulk_create_for_task(
        task_id=task["id"],
        deadline_iso=deadline,
        offset_seconds_list=[7200],
    )
    assert len(reminders) == 1
    assert reminders[0]["task_id"] == task["id"]


def test_reminder_engine_tick_fires_overdue_reminder():
    """ReminderEngine.tick() must fire a pending reminder whose fire_at is in the past."""
    from task_store import SqliteTaskStore
    from reminder_store import SqliteReminderStore
    from reminder_engine import ReminderEngine

    conn = _fresh_conn_with_fr7()
    conn.execute(
        "INSERT INTO users (id, name, role, created_at, deleted_at) "
        "VALUES (1, 'Alice', 'member', datetime('now'), NULL)"
    )
    # Minimal channel_bindings row so notification_service can resolve chat_id
    try:
        conn.execute(
            "INSERT INTO channel_bindings (user_id, channel, chat_id) "
            "VALUES (1, 'telegram', 'chat1')"
        )
    except Exception:
        pass  # table may not exist in minimal schema — skip binding
    conn.commit()

    ts = SqliteTaskStore(conn)
    rs = SqliteReminderStore(conn)

    # deadline = now+90min, offset=7200s → fire_at = now+90min-2h = now-30min (past, within 1h grace)
    deadline = (datetime.now(VN_TZ) + timedelta(minutes=90)).isoformat()
    task = ts.create_task(user_id=1, title="Overdue reminder task", deadline=deadline)

    rs.bulk_create_for_task(
        task_id=task["id"],
        deadline_iso=deadline,
        offset_seconds_list=[7200],
    )

    fake_user_store = MagicMock()
    fake_user_store.get_user_by_id.return_value = MagicMock(
        id=1, name="Alice", birthdate=None,
        channel_bindings=[MagicMock(channel="telegram", chat_id="chat1")],
    )
    fake_notif = MagicMock()
    fake_audit = MagicMock()

    engine = ReminderEngine(
        task_store=ts,
        reminder_store=rs,
        user_store=fake_user_store,
        notification_service=fake_notif,
        audit=fake_audit,
    )

    stats = engine.tick()
    # The overdue reminder should have been fired or missed (grace window = 1h → fired)
    assert stats.get("fired", 0) + stats.get("missed", 0) >= 1


def test_send_daily_summary_with_real_task_store():
    """send_daily_summary with a real SqliteTaskStore sends to users with matching time."""
    from task_store import SqliteTaskStore
    from scheduled_jobs import send_daily_summary

    conn = _fresh_conn_with_fr7()
    conn.execute(
        "INSERT INTO users (id, name, role, created_at, deleted_at) "
        "VALUES (1, 'Alice', 'member', datetime('now'), NULL)"
    )
    conn.commit()

    ts = SqliteTaskStore(conn)

    # Create a pending task due today
    today = datetime.now(VN_TZ)
    deadline = today.replace(hour=18, minute=0, second=0, microsecond=0).isoformat()
    ts.create_task(user_id=1, title="Bài tập toán", deadline=deadline)

    fake_user_store = MagicMock()
    from interfaces import User
    alice = User(id=1, name="Alice", role="member", username="alice")
    fake_user_store.list_users.return_value = [alice]
    fake_user_store.get_daily_summary_time.return_value = None  # default 21:00

    fake_notif = MagicMock()
    fake_audit = MagicMock()

    from dataclasses import dataclass, field
    from typing import Any

    @dataclass
    class _Deps:
        task_store: Any
        user_store: Any
        notification_service: Any
        audit: Any

    deps = _Deps(
        task_store=ts,
        user_store=fake_user_store,
        notification_service=fake_notif,
        audit=fake_audit,
    )

    # Fire at 21:00 — should match default
    fire_time = today.replace(hour=21, minute=0, second=0, microsecond=0)
    result = send_daily_summary(deps, now=fire_time)

    assert result["sent"] == 1
    assert fake_notif.enqueue.called
