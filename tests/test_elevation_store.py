"""tests/test_elevation_store.py — SqliteElevationStore unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from elevation_store import SqliteElevationStore


@pytest.fixture()
def estore(db_conn):
    """SqliteElevationStore wired to the in-memory connection."""
    return SqliteElevationStore(conn=db_conn)


# ── Session lifecycle ──────────────────────────────────────────────────────

def test_no_session_returns_none(estore):
    assert estore.get_active_session("telegram", "100") is None


def test_elevate_creates_active_session(estore, member_user):
    expires = estore.elevate("telegram", "100", base_user_id=member_user.id)
    assert expires  # ISO timestamp string
    session = estore.get_active_session("telegram", "100")
    assert session is not None
    assert session["base_user_id"] == member_user.id
    assert session["expires_at"] == expires


def test_elevate_refreshes_existing_session(estore, member_user):
    estore.elevate("telegram", "100", base_user_id=member_user.id, ttl_minutes=1)
    estore.elevate("telegram", "100", base_user_id=member_user.id, ttl_minutes=60)
    session = estore.get_active_session("telegram", "100")
    # New TTL should put expiry well in the future (> 30 min from now).
    expires = datetime.strptime(session["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    assert expires - datetime.now(timezone.utc) > timedelta(minutes=30)


def test_expired_session_returns_none(estore, member_user, db_conn):
    # Insert a session that has already expired.
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    db_conn.execute(
        "INSERT INTO elevation_sessions (channel, chat_id, base_user_id, expires_at) "
        "VALUES (?, ?, ?, ?)",
        ("telegram", "100", member_user.id, past),
    )
    db_conn.commit()
    assert estore.get_active_session("telegram", "100") is None


def test_drop_session_removes_row(estore, member_user):
    estore.elevate("telegram", "100", base_user_id=member_user.id)
    assert estore.drop_session("telegram", "100") is True
    assert estore.get_active_session("telegram", "100") is None
    # Idempotent — second drop returns False.
    assert estore.drop_session("telegram", "100") is False


def test_sessions_are_keyed_per_chat(estore, member_user, another_user):
    estore.elevate("telegram", "100", base_user_id=member_user.id)
    estore.elevate("telegram", "200", base_user_id=another_user.id)
    assert estore.get_active_session("telegram", "100")["base_user_id"] == member_user.id
    assert estore.get_active_session("telegram", "200")["base_user_id"] == another_user.id


# ── Rate limiting ──────────────────────────────────────────────────────────

def test_get_attempts_defaults_when_empty(estore):
    attempts = estore.get_attempts("telegram", "100")
    assert attempts["failed_count"] == 0
    assert attempts["locked_until"] is None


def test_record_failure_increments_count(estore):
    state = estore.record_failure("telegram", "100", max_fails=10)
    assert state["failed_count"] == 1
    assert state["locked_until"] is None
    state = estore.record_failure("telegram", "100", max_fails=10)
    assert state["failed_count"] == 2


def test_record_failure_locks_at_threshold(estore):
    for i in range(1, 5):
        state = estore.record_failure(
            "telegram", "100", max_fails=5, lockout_minutes=15
        )
        assert state["locked_until"] is None, f"locked too early at attempt {i}"
    state = estore.record_failure(
        "telegram", "100", max_fails=5, lockout_minutes=15
    )
    assert state["failed_count"] == 5
    assert state["locked_until"] is not None


def test_is_locked_true_after_lockout(estore):
    for _ in range(5):
        estore.record_failure("telegram", "100", max_fails=5, lockout_minutes=15)
    locked, until = estore.is_locked("telegram", "100")
    assert locked is True
    assert until is not None


def test_is_locked_false_when_lock_expired(estore, db_conn):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    db_conn.execute(
        "INSERT INTO sudo_attempts "
        "(channel, chat_id, failed_count, locked_until, last_attempt_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("telegram", "100", 5, past, past),
    )
    db_conn.commit()
    locked, _ = estore.is_locked("telegram", "100")
    assert locked is False


def test_reset_failures_clears_lock_and_count(estore):
    for _ in range(5):
        estore.record_failure("telegram", "100", max_fails=5, lockout_minutes=15)
    estore.reset_failures("telegram", "100")
    attempts = estore.get_attempts("telegram", "100")
    assert attempts["failed_count"] == 0
    assert attempts["locked_until"] is None


def test_default_thresholds_come_from_config(estore, monkeypatch):
    """Calling record_failure without explicit args uses config.SUDO_MAX_FAILS."""
    monkeypatch.setattr(config, "SUDO_MAX_FAILS", 3)
    monkeypatch.setattr(config, "SUDO_LOCKOUT_MINUTES", 5)
    for i in range(1, 3):
        state = estore.record_failure("telegram", "100")
        assert state["locked_until"] is None, f"locked too early at attempt {i}"
    state = estore.record_failure("telegram", "100")
    assert state["locked_until"] is not None
