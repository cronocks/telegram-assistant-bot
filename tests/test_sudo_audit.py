"""Tests for FR-4 sub-4.1b — sudo handler audit emission.

These verify that each sudo lifecycle event (elevate, drop, fail, locked,
password_set) writes a row into `audit_log` in addition to the existing stdout
print line. Stdout is intentionally kept in parallel during early FR-4 per plan
decision D8, but the assertions here only check the audit table.

We exercise the real handlers (`_cmd_sudo`, `_cmd_thoat_sudo`, `_cmd_dat_mat_khau`)
with a FakeChannel + a real SqliteAuditLog wired through CoreDeps.
"""
from __future__ import annotations

import pytest

from audit import SqliteAuditLog
from deps import CoreDeps
from cmd_sudo import (
    _cmd_dat_mat_khau,
    _cmd_sudo,
    _cmd_thoat_sudo,
)
from elevation_store import SqliteElevationStore


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, int]] = []

    async def send(self, chat_id: str, text: str, use_markdown: bool = True) -> None:
        self.sent.append((chat_id, text))

    async def delete_message(self, chat_id: str, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True


@pytest.fixture()
def audit(db_conn):
    return SqliteAuditLog(conn=db_conn)


@pytest.fixture()
def estore(db_conn):
    return SqliteElevationStore(conn=db_conn)


def _make_deps(store, audit, estore) -> CoreDeps:
    return CoreDeps(
        llm=None,  # type: ignore[arg-type]
        notes=None,  # type: ignore[arg-type]
        wiki=None,  # type: ignore[arg-type]
        channel=FakeChannel(),
        user_store=store,
        note_index=None,  # type: ignore[arg-type]
        memory_store=None,  # type: ignore[arg-type]
        elevation_store=estore,
        audit=audit,
    )


@pytest.fixture()
def admin_with_password(store, sample_admin):
    store.set_password(sample_admin.id, "correct-horse-battery")
    return sample_admin


@pytest.fixture()
def manager_user(store):
    return store.create_user(name="Manager User", role="manager")


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# ── sudo_elevate ──────────────────────────────────────────────────────────────

def test_audit_sudo_elevate_on_success(
    store, audit, estore, admin_with_password, manager_user
):
    deps = _make_deps(store, audit, estore)
    _run(_cmd_sudo("999", "correct-horse-battery", manager_user, message_id=1, deps=deps))

    events = audit.list_recent(action="sudo_elevate")
    assert len(events) == 1
    assert events[0].actor_user_id == manager_user.id
    assert events[0].payload["matched_admin"] == admin_with_password.id
    assert "expires_at" in events[0].payload


# ── sudo_fail — wrong password ────────────────────────────────────────────────

def test_audit_sudo_fail_on_wrong_password(
    store, audit, estore, admin_with_password, manager_user
):
    deps = _make_deps(store, audit, estore)
    _run(_cmd_sudo("999", "totally-wrong", manager_user, message_id=1, deps=deps))

    events = audit.list_recent(action="sudo_fail")
    assert len(events) == 1
    assert events[0].actor_user_id == manager_user.id
    assert events[0].payload["reason"] == "wrong_password"
    assert events[0].payload["failed_count"] == 1


# ── sudo_fail — role gating ───────────────────────────────────────────────────

def test_audit_sudo_fail_when_caller_not_manager(
    store, audit, estore, admin_with_password, member_user
):
    deps = _make_deps(store, audit, estore)
    _run(_cmd_sudo("999", "correct-horse-battery", member_user, message_id=1, deps=deps))

    events = audit.list_recent(action="sudo_fail")
    assert len(events) == 1
    assert events[0].payload["reason"] == "role_not_manager"
    assert events[0].payload["role"] == "member"


# ── sudo_locked ───────────────────────────────────────────────────────────────

def test_audit_sudo_locked_when_lockout_active(
    store, audit, estore, admin_with_password, manager_user
):
    # Force a lockout via repeated failures from elevation_store directly.
    import config
    for _ in range(config.SUDO_MAX_FAILS):
        estore.record_failure("telegram", "999")

    deps = _make_deps(store, audit, estore)
    _run(_cmd_sudo("999", "correct-horse-battery", manager_user, message_id=1, deps=deps))

    events = audit.list_recent(action="sudo_locked")
    assert len(events) == 1
    assert events[0].actor_user_id == manager_user.id
    assert "locked_until" in events[0].payload


# ── sudo_drop ─────────────────────────────────────────────────────────────────

def test_audit_sudo_drop_on_thoat_sudo(
    store, audit, estore, admin_with_password, manager_user
):
    # Set up an active session first.
    estore.elevate("telegram", "999", base_user_id=manager_user.id)

    deps = _make_deps(store, audit, estore)
    _run(_cmd_thoat_sudo("999", manager_user, deps))

    events = audit.list_recent(action="sudo_drop")
    assert len(events) == 1
    assert events[0].actor_user_id == manager_user.id


def test_no_audit_when_thoat_sudo_with_no_active_session(
    store, audit, estore, manager_user
):
    deps = _make_deps(store, audit, estore)
    _run(_cmd_thoat_sudo("999", manager_user, deps))

    # No drop happened → no audit row.
    assert audit.list_recent(action="sudo_drop") == []


# ── password_set ──────────────────────────────────────────────────────────────

def test_audit_password_set_by_native_admin(
    store, audit, estore, sample_admin
):
    deps = _make_deps(store, audit, estore)
    _run(_cmd_dat_mat_khau("999", "new-password-1234", sample_admin, message_id=1, deps=deps))

    events = audit.list_recent(action="password_set")
    assert len(events) == 1
    assert events[0].actor_user_id == sample_admin.id
    assert events[0].target_type == "user"
    assert events[0].target_id == str(sample_admin.id)
    assert events[0].payload["name"] == sample_admin.name


# ── End-to-end audit trail ────────────────────────────────────────────────────

def test_full_sudo_cycle_writes_three_audit_rows(
    store, audit, estore, admin_with_password, manager_user
):
    """elevate → drop produces exactly elevate + drop audit rows."""
    deps = _make_deps(store, audit, estore)
    _run(_cmd_sudo("999", "correct-horse-battery", manager_user, message_id=1, deps=deps))
    _run(_cmd_thoat_sudo("999", manager_user, deps))

    actions = [e.action for e in audit.list_recent(limit=10)]
    # DESC order → drop first, then elevate.
    assert actions == ["sudo_drop", "sudo_elevate"]
