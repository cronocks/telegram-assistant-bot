"""Tests for FR-9 scheduled jobs: weekly ledger summary + 30-day voided purge."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from budget_store import SqliteBudgetStore
from category_store import SqliteCategoryStore
from ledger_reports import LedgerReports
from ledger_store import SqliteLedgerStore
from scheduled_jobs import purge_voided_ledger_entries, send_weekly_ledger_summary


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


def _make_deps(conn: sqlite3.Connection, users: list[dict] | None = None):
    for u in (users or []):
        conn.execute(
            "INSERT INTO users (id, name, role) VALUES (?, ?, ?)",
            (u["id"], u["name"], u.get("role", "member")),
        )
    conn.commit()

    ledger_store = SqliteLedgerStore(conn=conn)
    category_store = SqliteCategoryStore(conn=conn)
    budget_store = SqliteBudgetStore(conn=conn)
    ledger_reports = LedgerReports(ledger_store, budget_store)

    notification_service = MagicMock()
    notification_service.enqueue = MagicMock()

    user_store = MagicMock()
    user_store.list_users.return_value = [
        MagicMock(id=u["id"], name=u["name"]) for u in (users or [])
    ]

    deps = MagicMock()
    deps.ledger_store = ledger_store
    deps.category_store = category_store
    deps.budget_store = budget_store
    deps.ledger_reports = ledger_reports
    deps.notification_service = notification_service
    deps.user_store = user_store

    return deps, ledger_store, category_store


# ── send_weekly_ledger_summary ────────────────────────────────────────────────


def test_weekly_summary_no_entries_skips(tmp_path):
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    # Monday 08:00 VN
    now = datetime(2026, 5, 25, 1, 0, 0, tzinfo=timezone.utc)  # 08:00 VN = 01:00 UTC
    result = send_weekly_ledger_summary(deps, now=now)

    assert result["sent"] == 0
    assert result["skipped"] == 1
    deps.notification_service.enqueue.assert_not_called()


def test_weekly_summary_sends_when_entries_exist():
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    # Add an entry 3 days ago
    now = datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc)  # Mon 08:00 VN
    three_days_ago = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    ledger_store.add_entry(1, "expense", 50000, three_days_ago, source="telegram")

    result = send_weekly_ledger_summary(deps, now=now)

    assert result["sent"] == 1
    assert result["skipped"] == 0
    deps.notification_service.enqueue.assert_called_once()
    call_args = deps.notification_service.enqueue.call_args
    assert call_args[0][0] == 1  # user_id
    text = call_args[0][2]["text"]
    assert "50.000" in text or "50000" in text


def test_weekly_summary_skips_when_no_ledger_reports():
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    now = datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc)
    ledger_store.add_entry(1, "expense", 50000, "2026-05-24 10:00:00", source="telegram")
    deps.ledger_reports = None

    result = send_weekly_ledger_summary(deps, now=now)
    assert result == {"sent": 0, "skipped": 0}


def test_weekly_summary_skips_when_no_notification_service():
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    now = datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc)
    ledger_store.add_entry(1, "expense", 50000, "2026-05-24 10:00:00", source="telegram")
    deps.notification_service = None

    result = send_weekly_ledger_summary(deps, now=now)
    assert result == {"sent": 0, "skipped": 0}


def test_weekly_summary_multiple_users_independent():
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    now = datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc)
    # Alice has entry, Bob does not
    ledger_store.add_entry(1, "income", 200000, "2026-05-24 09:00:00", source="telegram")

    result = send_weekly_ledger_summary(deps, now=now)
    assert result["sent"] == 1
    assert result["skipped"] == 1
    assert deps.notification_service.enqueue.call_count == 1


def test_weekly_summary_includes_income_and_expense():
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    now = datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc)
    ledger_store.add_entry(1, "income", 5000000, "2026-05-24 09:00:00", source="telegram")
    ledger_store.add_entry(1, "expense", 300000, "2026-05-24 12:00:00", source="telegram")

    result = send_weekly_ledger_summary(deps, now=now)
    assert result["sent"] == 1
    text = deps.notification_service.enqueue.call_args[0][2]["text"]
    # Should mention both income and expense figures
    assert "5.000.000" in text or "5000000" in text
    assert "300.000" in text or "300000" in text


# ── purge_voided_ledger_entries ───────────────────────────────────────────────


def test_purge_voided_removes_old_entries():
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    # Add and void an entry 31 days ago
    entry = ledger_store.add_entry(1, "expense", 10000, "2026-04-20 10:00:00", source="telegram")
    old_void_time = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE ledger_entries SET voided_at = ? WHERE id = ?",
        (old_void_time, entry["id"]),
    )
    conn.commit()

    result = purge_voided_ledger_entries(deps)
    assert result["purged"] == 1

    # Entry should be gone
    row = conn.execute("SELECT * FROM ledger_entries WHERE id = ?", (entry["id"],)).fetchone()
    assert row is None


def test_purge_voided_keeps_recent_voided():
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    # Void an entry just 5 days ago — should NOT be purged
    entry = ledger_store.add_entry(1, "expense", 20000, "2026-05-20 10:00:00", source="telegram")
    recent_void = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE ledger_entries SET voided_at = ? WHERE id = ?",
        (recent_void, entry["id"]),
    )
    conn.commit()

    result = purge_voided_ledger_entries(deps)
    assert result["purged"] == 0

    row = conn.execute("SELECT * FROM ledger_entries WHERE id = ?", (entry["id"],)).fetchone()
    assert row is not None


def test_purge_voided_keeps_active_entries():
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    # Active (not voided) entry — must never be purged
    entry = ledger_store.add_entry(1, "income", 100000, "2026-04-01 10:00:00", source="telegram")

    result = purge_voided_ledger_entries(deps)
    assert result["purged"] == 0

    row = conn.execute("SELECT * FROM ledger_entries WHERE id = ?", (entry["id"],)).fetchone()
    assert row is not None


def test_purge_voided_skips_when_no_ledger_store():
    conn = _make_db()
    deps, _, _ = _make_deps(conn, [])
    deps.ledger_store = None

    result = purge_voided_ledger_entries(deps)
    assert result == {"purged": 0}


def test_purge_voided_mixed_batch():
    conn = _make_db()
    users = [{"id": 1, "name": "Alice"}]
    deps, ledger_store, _ = _make_deps(conn, users)

    # 2 old voided, 1 recent voided, 1 active
    for i in range(2):
        e = ledger_store.add_entry(1, "expense", 1000 * (i + 1), f"2026-04-0{i+1} 10:00:00", source="telegram")
        old_void = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE ledger_entries SET voided_at = ? WHERE id = ?", (old_void, e["id"]))

    recent = ledger_store.add_entry(1, "expense", 5000, "2026-05-20 10:00:00", source="telegram")
    conn.execute(
        "UPDATE ledger_entries SET voided_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"), recent["id"]),
    )
    ledger_store.add_entry(1, "income", 99999, "2026-05-25 10:00:00", source="telegram")
    conn.commit()

    result = purge_voided_ledger_entries(deps)
    assert result["purged"] == 2
    assert conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0] == 2
