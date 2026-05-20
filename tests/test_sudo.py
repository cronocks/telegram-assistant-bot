"""tests/test_sudo.py — integration tests for the sudo elevation flow.

These cover the password-verification + session-creation integration between
SqliteUserStore (FR-2 password infrastructure) and SqliteElevationStore. The
core_handler command-level smoke tests are deferred to staging since they
require mocking the full async ChannelAdapter and LLMClient surface.
"""
from __future__ import annotations

import pytest

from elevation_store import SqliteElevationStore


@pytest.fixture()
def estore(db_conn):
    return SqliteElevationStore(conn=db_conn)


@pytest.fixture()
def admin_with_password(store, sample_admin):
    """Admin user with a known password set."""
    store.set_password(sample_admin.id, "correct-horse-battery")
    return sample_admin


@pytest.fixture()
def manager_user(store):
    """A manager user — the typical sudo caller."""
    return store.create_user(name="Manager User", role="manager")


# ── Successful elevation ───────────────────────────────────────────────────

def test_elevate_succeeds_with_correct_password(
    estore, store, admin_with_password, manager_user
):
    assert store.check_password(admin_with_password.id, "correct-horse-battery") is True
    estore.elevate("telegram", "999", base_user_id=manager_user.id)
    session = estore.get_active_session("telegram", "999")
    assert session is not None
    # Session keys the manager, not the matched admin.
    assert session["base_user_id"] == manager_user.id


def test_reset_failures_after_successful_elevate(
    estore, store, admin_with_password, manager_user
):
    # Two prior failures should be cleared on success.
    estore.record_failure("telegram", "999", max_fails=5, lockout_minutes=15)
    estore.record_failure("telegram", "999", max_fails=5, lockout_minutes=15)
    assert estore.get_attempts("telegram", "999")["failed_count"] == 2

    estore.reset_failures("telegram", "999")
    estore.elevate("telegram", "999", base_user_id=manager_user.id)
    assert estore.get_attempts("telegram", "999")["failed_count"] == 0


# ── Wrong password / lockout ───────────────────────────────────────────────

def test_wrong_password_does_not_create_session(
    estore, store, admin_with_password
):
    assert store.check_password(admin_with_password.id, "wrong") is False
    assert estore.get_active_session("telegram", "999") is None


def test_lockout_after_max_fails(estore):
    for _ in range(5):
        estore.record_failure("telegram", "999", max_fails=5, lockout_minutes=15)
    locked, until = estore.is_locked("telegram", "999")
    assert locked is True
    assert until is not None


# ── Multi-admin password verification ──────────────────────────────────────

def test_password_matches_any_admin(store, sample_admin):
    second_admin = store.create_user(name="Second Admin", role="admin")
    store.set_password(sample_admin.id, "first-pw")
    store.set_password(second_admin.id, "second-pw")

    # Either admin's password should match its own hash.
    assert store.check_password(sample_admin.id, "first-pw") is True
    assert store.check_password(second_admin.id, "second-pw") is True
    # Cross-checks fail.
    assert store.check_password(sample_admin.id, "second-pw") is False
    assert store.check_password(second_admin.id, "first-pw") is False


def test_admin_without_password_cannot_be_matched(store, sample_admin):
    # No set_password call → check_password returns False.
    assert store.check_password(sample_admin.id, "anything") is False


# ── Drop session ───────────────────────────────────────────────────────────

def test_drop_session_after_elevate(estore, manager_user):
    estore.elevate("telegram", "999", base_user_id=manager_user.id)
    assert estore.drop_session("telegram", "999") is True
    assert estore.get_active_session("telegram", "999") is None
