"""Tests for notification_service.NotificationService (FR-4 sub 4.5).

Covers the flush_pending loop and its audit trail:
  - immediate success → notification_delivered
  - fail → retry with next_retry_at set → notification_retry audit per attempt
  - fail × max_attempts → notification_failed
  - full retry trace: enqueue + retry×2 + delivered (4 audit rows in order)
  - no channel adapter registered → treated as retryable error
  - no chat_id binding → treated as retryable error
  - empty text in payload → treated as retryable error
  - invalid JSON payload → treated as retryable error
  - rows not yet due are skipped
  - enqueue emits notification_enqueued audit row
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from audit import SqliteAuditLog
from notification_service import NotificationService
from notification_store import SqliteNotificationStore


T0 = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeChannel:
    """Async channel adapter stub."""

    def __init__(self, *, succeed: bool = True, raises: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._succeed = succeed
        self._raises = raises

    async def send(self, chat_id: str, text: str, use_markdown: bool = False) -> None:
        self.calls.append((chat_id, text))
        if self._raises is not None:
            raise self._raises
        if not self._succeed:
            raise RuntimeError("send failed")


class FakeUserStore:
    """Minimal UserStore stub for chat_id lookup."""

    def __init__(self, binding: str | None = "chat-42") -> None:
        self._binding = binding

    def get_chat_id_for_user(self, user_id: int, channel: str) -> str | None:
        return self._binding


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def notif_store(db_conn):
    return SqliteNotificationStore(conn=db_conn)


@pytest.fixture()
def audit_log(db_conn):
    return SqliteAuditLog(conn=db_conn)


@pytest.fixture()
def user_id(store):
    u = store.create_user(name="Notif User", role="member")
    return u.id


def _make_service(
    notif_store,
    audit_log,
    channel: FakeChannel | None = None,
    user_store: FakeUserStore | None = None,
    max_attempts: int = 5,
) -> NotificationService:
    if channel is None:
        channel = FakeChannel(succeed=True)
    if user_store is None:
        user_store = FakeUserStore(binding="chat-42")
    return NotificationService(
        store=notif_store,
        audit=audit_log,
        user_store=user_store,
        channels={"telegram": channel},
        max_attempts=max_attempts,
    )


# ═════════════════════════════════════════════════════════════════════════════
# enqueue — audit
# ═════════════════════════════════════════════════════════════════════════════


class TestEnqueueAudit:

    def test_enqueue_emits_notification_enqueued(self, notif_store, audit_log, user_id):
        svc = _make_service(notif_store, audit_log)
        nid = svc.enqueue(user_id, "telegram", {"kind": "reminder", "text": "hi"})
        events = audit_log.list_recent(action="notification_enqueued")
        assert len(events) == 1
        e = events[0]
        assert e.target_id == str(nid)
        assert e.payload["user_id"] == user_id
        assert e.payload["channel"] == "telegram"
        assert e.payload["kind"] == "reminder"


# ═════════════════════════════════════════════════════════════════════════════
# flush_pending — success
# ═════════════════════════════════════════════════════════════════════════════


class TestFlushSuccess:

    def test_delivers_pending_notification(self, notif_store, audit_log, user_id):
        svc = _make_service(notif_store, audit_log)
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hello"})

        summary = asyncio.run(
            svc.flush_pending(now=T0)
        )

        assert summary["delivered"] == 1
        assert notif_store.get_by_id(nid)["status"] == "delivered"

    def test_delivered_emits_notification_delivered_audit(
        self, notif_store, audit_log, user_id,
    ):
        svc = _make_service(notif_store, audit_log)
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hello"})
        asyncio.run(svc.flush_pending(now=T0))

        events = audit_log.list_recent(action="notification_delivered")
        assert len(events) == 1
        assert events[0].target_id == str(nid)
        assert events[0].payload["total_attempts"] == 1

    def test_sends_to_correct_chat_id(self, notif_store, audit_log, user_id):
        channel = FakeChannel(succeed=True)
        svc = _make_service(notif_store, audit_log, channel=channel,
                            user_store=FakeUserStore(binding="chat-99"))
        notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "msg"})
        asyncio.run(svc.flush_pending(now=T0))

        assert channel.calls == [("chat-99", "msg")]

    def test_skips_not_yet_due(self, notif_store, audit_log, user_id):
        svc = _make_service(notif_store, audit_log)
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "later"})
        future = T0 + timedelta(minutes=10)
        notif_store._conn.execute(
            "UPDATE pending_notifications SET next_retry_at = ? WHERE id = ?",
            (future.strftime("%Y-%m-%d %H:%M:%S"), nid),
        )
        notif_store._conn.commit()

        summary = asyncio.run(svc.flush_pending(now=T0))
        assert summary["processed"] == 0
        assert notif_store.get_by_id(nid)["status"] == "pending"

    def test_empty_queue_returns_zero_summary(self, notif_store, audit_log):
        svc = _make_service(notif_store, audit_log)
        summary = asyncio.run(svc.flush_pending(now=T0))
        assert summary == {"delivered": 0, "retried": 0, "failed": 0, "processed": 0}


# ═════════════════════════════════════════════════════════════════════════════
# flush_pending — retries
# ═════════════════════════════════════════════════════════════════════════════


class TestFlushRetry:

    def test_fail_increments_attempts_and_schedules_retry(
        self, notif_store, audit_log, user_id,
    ):
        channel = FakeChannel(succeed=False)
        svc = _make_service(notif_store, audit_log, channel=channel, max_attempts=5)
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})

        summary = asyncio.run(svc.flush_pending(now=T0))

        assert summary["retried"] == 1
        row = notif_store.get_by_id(nid)
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert row["next_retry_at"] is not None

    def test_fail_emits_notification_retry_audit(self, notif_store, audit_log, user_id):
        channel = FakeChannel(succeed=False)
        svc = _make_service(notif_store, audit_log, channel=channel, max_attempts=5)
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        asyncio.run(svc.flush_pending(now=T0))

        events = audit_log.list_recent(action="notification_retry")
        assert len(events) == 1
        assert events[0].target_id == str(nid)
        assert events[0].payload["attempt"] == 1

    def test_final_fail_after_max_attempts(self, notif_store, audit_log, user_id):
        channel = FakeChannel(succeed=False)
        svc = _make_service(notif_store, audit_log, channel=channel, max_attempts=3)
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})

        now = T0
        for _ in range(3):
            asyncio.run(svc.flush_pending(now=now))
            # Advance time past the retry window.
            now = now + timedelta(hours=1)
            notif_store._conn.execute(
                "UPDATE pending_notifications SET next_retry_at = NULL WHERE id = ?",
                (nid,),
            )
            notif_store._conn.commit()

        row = notif_store.get_by_id(nid)
        assert row["status"] == "failed"
        events = audit_log.list_recent(action="notification_failed")
        assert len(events) == 1
        assert events[0].target_id == str(nid)


# ═════════════════════════════════════════════════════════════════════════════
# flush_pending — full retry trace (FR-4-PLAN §11.1)
# ═════════════════════════════════════════════════════════════════════════════


class TestFlushRetryTrace:

    def test_fail_twice_then_succeed_produces_correct_audit_trail(
        self, notif_store, audit_log, user_id,
    ):
        """4 audit rows: enqueued, retry×2, delivered — in order."""
        call_count = 0

        class FlakyChannel:
            calls: list = []

            async def send(self, chat_id: str, text: str, use_markdown: bool = False):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise RuntimeError(f"fail {call_count}")

        channel = FlakyChannel()
        svc = NotificationService(
            store=notif_store,
            audit=audit_log,
            user_store=FakeUserStore(binding="chat-1"),
            channels={"telegram": channel},
            max_attempts=5,
        )
        nid = svc.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})

        now = T0
        for _ in range(3):
            asyncio.run(svc.flush_pending(now=now))
            now = now + timedelta(hours=1)
            notif_store._conn.execute(
                "UPDATE pending_notifications SET next_retry_at = NULL WHERE id = ?",
                (nid,),
            )
            notif_store._conn.commit()

        # list_recent returns DESC; reverse for chronological order.
        all_events = list(reversed(audit_log.list_recent()))
        notif_events = [e for e in all_events if e.target_id == str(nid)]

        actions = [e.action for e in notif_events]
        assert actions == [
            "notification_enqueued",
            "notification_retry",
            "notification_retry",
            "notification_delivered",
        ]
        # retry payloads carry attempt number.
        assert notif_events[1].payload["attempt"] == 1
        assert notif_events[2].payload["attempt"] == 2
        # delivered payload carries total_attempts.
        assert notif_events[3].payload["total_attempts"] == 3


# ═════════════════════════════════════════════════════════════════════════════
# flush_pending — error cases (all treated as retryable)
# ═════════════════════════════════════════════════════════════════════════════


class TestFlushErrorCases:

    def test_no_adapter_for_channel_schedules_retry(
        self, notif_store, audit_log, user_id,
    ):
        svc = NotificationService(
            store=notif_store,
            audit=audit_log,
            user_store=FakeUserStore(),
            channels={},  # no "telegram" adapter
            max_attempts=5,
        )
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        summary = asyncio.run(svc.flush_pending(now=T0))
        assert summary["retried"] == 1
        assert notif_store.get_by_id(nid)["status"] == "pending"

    def test_no_chat_id_binding_schedules_retry(self, notif_store, audit_log, user_id):
        svc = _make_service(
            notif_store, audit_log,
            user_store=FakeUserStore(binding=None),  # no binding
        )
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        summary = asyncio.run(svc.flush_pending(now=T0))
        assert summary["retried"] == 1

    def test_empty_text_schedules_retry(self, notif_store, audit_log, user_id):
        svc = _make_service(notif_store, audit_log)
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": ""})
        summary = asyncio.run(svc.flush_pending(now=T0))
        assert summary["retried"] == 1

    def test_invalid_json_payload_schedules_retry(self, notif_store, audit_log, user_id):
        svc = _make_service(notif_store, audit_log)
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "ok"})
        # Corrupt the payload directly.
        notif_store._conn.execute(
            "UPDATE pending_notifications SET payload = 'not-json' WHERE id = ?",
            (nid,),
        )
        notif_store._conn.commit()
        summary = asyncio.run(svc.flush_pending(now=T0))
        assert summary["retried"] == 1
