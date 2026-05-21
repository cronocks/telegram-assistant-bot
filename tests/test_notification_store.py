"""Tests for notification_store.SqliteNotificationStore (FR-4 sub 4.5).

Covers:
  - enqueue: insert returns a positive id, row is readable via get_by_id
  - get_pending_ready: status/next_retry_at filtering + FIFO ordering
  - mark_delivered: status transition, idempotent on already-delivered
  - record_failed_attempt: retry scheduling with exponential backoff,
    final-fail transition after max_attempts
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from notification_store import SqliteNotificationStore


T0 = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def notif_store(db_conn):
    return SqliteNotificationStore(conn=db_conn)


@pytest.fixture()
def user_id(store):
    """A real user id required by the FK on pending_notifications."""
    u = store.create_user(name="Notif User", role="member")
    return u.id


# ═════════════════════════════════════════════════════════════════════════════
# enqueue
# ═════════════════════════════════════════════════════════════════════════════


class TestEnqueue:

    def test_returns_positive_id(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "test", "text": "hi"})
        assert isinstance(nid, int) and nid > 0

    def test_row_readable_via_get_by_id(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "test", "text": "hello"})
        row = notif_store.get_by_id(nid)
        assert row is not None
        assert row["id"] == nid
        assert row["user_id"] == user_id
        assert row["channel"] == "telegram"
        assert row["status"] == "pending"
        assert row["attempts"] == 0

    def test_payload_roundtrips_as_json_string(self, notif_store, user_id):
        import json
        payload = {"kind": "reminder", "text": "Test message", "extra": 42}
        nid = notif_store.enqueue(user_id, "telegram", payload)
        row = notif_store.get_by_id(nid)
        assert json.loads(row["payload"]) == payload

    def test_empty_channel_raises(self, notif_store, user_id):
        with pytest.raises(ValueError):
            notif_store.enqueue(user_id, "", {"kind": "x", "text": "y"})

    def test_sequential_ids_are_distinct(self, notif_store, user_id):
        ids = [notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "t"}) for _ in range(3)]
        assert len(set(ids)) == 3


# ═════════════════════════════════════════════════════════════════════════════
# get_pending_ready
# ═════════════════════════════════════════════════════════════════════════════


class TestGetPendingReady:

    def test_returns_new_notification_immediately(self, notif_store, user_id):
        notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        rows = notif_store.get_pending_ready(now=T0)
        assert len(rows) == 1

    def test_excludes_delivered(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        notif_store.mark_delivered(nid, now=T0)
        assert notif_store.get_pending_ready(now=T0) == []

    def test_excludes_not_yet_due(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        # Manually set next_retry_at 10 minutes in the future.
        future = T0 + timedelta(minutes=10)
        notif_store._conn.execute(
            "UPDATE pending_notifications SET next_retry_at = ? WHERE id = ?",
            (future.strftime("%Y-%m-%d %H:%M:%S"), nid),
        )
        notif_store._conn.commit()
        assert notif_store.get_pending_ready(now=T0) == []

    def test_includes_past_due(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        past = T0 - timedelta(minutes=5)
        notif_store._conn.execute(
            "UPDATE pending_notifications SET next_retry_at = ? WHERE id = ?",
            (past.strftime("%Y-%m-%d %H:%M:%S"), nid),
        )
        notif_store._conn.commit()
        rows = notif_store.get_pending_ready(now=T0)
        assert len(rows) == 1

    def test_fifo_order(self, notif_store, user_id):
        a = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "a"})
        b = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "b"})
        rows = notif_store.get_pending_ready(now=T0)
        assert [r["id"] for r in rows] == [a, b]

    def test_limit_respected(self, notif_store, user_id):
        for _ in range(5):
            notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "x"})
        rows = notif_store.get_pending_ready(now=T0, limit=3)
        assert len(rows) == 3

    def test_limit_zero_returns_empty(self, notif_store, user_id):
        notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "x"})
        assert notif_store.get_pending_ready(now=T0, limit=0) == []


# ═════════════════════════════════════════════════════════════════════════════
# mark_delivered
# ═════════════════════════════════════════════════════════════════════════════


class TestMarkDelivered:

    def test_transitions_to_delivered(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        assert notif_store.mark_delivered(nid, now=T0) is True
        assert notif_store.get_by_id(nid)["status"] == "delivered"

    def test_sets_delivered_at(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        notif_store.mark_delivered(nid, now=T0)
        row = notif_store.get_by_id(nid)
        assert row["delivered_at"] is not None

    def test_idempotent_on_already_delivered(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        notif_store.mark_delivered(nid, now=T0)
        # Second call returns False (no row updated) but doesn't raise.
        assert notif_store.mark_delivered(nid, now=T0) is False


# ═════════════════════════════════════════════════════════════════════════════
# record_failed_attempt — retry backoff
# ═════════════════════════════════════════════════════════════════════════════


class TestRecordFailedAttempt:

    def test_increments_attempts(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        result = notif_store.record_failed_attempt(nid, "err1", max_attempts=5, now=T0)
        assert result["attempts"] == 1
        assert notif_store.get_by_id(nid)["attempts"] == 1

    def test_status_stays_pending_before_max(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        result = notif_store.record_failed_attempt(nid, "err", max_attempts=5, now=T0)
        assert result["final"] is False
        assert notif_store.get_by_id(nid)["status"] == "pending"

    def test_exponential_backoff_first_retry(self, notif_store, user_id):
        # After 1st failure (attempts=0→1): next_retry = T0 + 2^1 = T0 + 2 min.
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        result = notif_store.record_failed_attempt(nid, "err", max_attempts=5, now=T0)
        expected = (T0 + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        assert result["next_retry_at"] == expected

    def test_exponential_backoff_second_retry(self, notif_store, user_id):
        # After 2nd failure (attempts=1→2): next_retry = now + 2^2 = now + 4 min.
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        notif_store.record_failed_attempt(nid, "err1", max_attempts=5, now=T0)
        t1 = T0 + timedelta(minutes=5)
        result = notif_store.record_failed_attempt(nid, "err2", max_attempts=5, now=t1)
        expected = (t1 + timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M:%S")
        assert result["next_retry_at"] == expected

    def test_final_fail_after_max_attempts(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        result = None
        for i in range(5):
            result = notif_store.record_failed_attempt(
                nid, f"err{i}", max_attempts=5, now=T0 + timedelta(minutes=i),
            )
        assert result["final"] is True
        assert result["next_retry_at"] is None
        row = notif_store.get_by_id(nid)
        assert row["status"] == "failed"
        assert row["attempts"] == 5

    def test_last_error_stored(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        notif_store.record_failed_attempt(nid, "network timeout", max_attempts=5, now=T0)
        assert notif_store.get_by_id(nid)["last_error"] == "network timeout"

    def test_error_truncated_to_500_chars(self, notif_store, user_id):
        nid = notif_store.enqueue(user_id, "telegram", {"kind": "k", "text": "hi"})
        long_err = "x" * 600
        notif_store.record_failed_attempt(nid, long_err, max_attempts=5, now=T0)
        assert len(notif_store.get_by_id(nid)["last_error"]) == 500

    def test_raises_on_missing_id(self, notif_store):
        with pytest.raises(ValueError):
            notif_store.record_failed_attempt(9999, "err", max_attempts=5, now=T0)
