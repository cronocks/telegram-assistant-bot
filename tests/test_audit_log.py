"""Tests for SqliteAuditLog — FR-4 audit log adapter."""
import pytest

from audit import AuditEvent, SqliteAuditLog


@pytest.fixture()
def audit(db_conn):
    """SqliteAuditLog wired to the in-memory connection."""
    return SqliteAuditLog(conn=db_conn)


class TestLog:
    def test_log_basic_insert(self, audit, sample_admin):
        rid = audit.log(sample_admin.id, "sudo_elevate")
        assert isinstance(rid, int)
        assert rid > 0

    def test_log_with_payload(self, audit, sample_admin):
        rid = audit.log(
            sample_admin.id,
            "notification_retry",
            target_type="notification",
            target_id=42,
            payload={"attempt": 1, "error": "timeout", "next_retry_at": "21:02:30"},
        )
        events = audit.list_recent(limit=1)
        assert events[0].id == rid
        assert events[0].payload == {
            "attempt": 1,
            "error": "timeout",
            "next_retry_at": "21:02:30",
        }

    def test_log_actor_none_is_system_event(self, audit):
        audit.log(None, "auto_purge_18", target_type="user", target_id=7)
        events = audit.list_recent(limit=1)
        assert events[0].actor_user_id is None
        assert events[0].action == "auto_purge_18"

    def test_log_target_id_coerced_to_text(self, audit, sample_admin):
        """Integer note ids and string drive_file_ids share the same column."""
        audit.log(sample_admin.id, "stealth_read_note", target_type="note", target_id=12)
        audit.log(sample_admin.id, "stealth_read_note", target_type="note", target_id="drive_xyz")
        events = audit.list_recent(limit=2)
        ids = {e.target_id for e in events}
        assert ids == {"12", "drive_xyz"}

    def test_log_rejects_empty_action(self, audit, sample_admin):
        with pytest.raises(ValueError):
            audit.log(sample_admin.id, "")


class TestListRecent:
    def test_returns_empty_when_no_rows(self, audit):
        assert audit.list_recent() == []

    def test_order_is_desc_by_time_then_id(self, audit, sample_admin):
        # Three events in insertion order.
        ids = [audit.log(sample_admin.id, f"action_{i}") for i in range(3)]
        events = audit.list_recent(limit=10)
        # Most recent first → matches reverse insertion order.
        assert [e.id for e in events] == list(reversed(ids))

    def test_filter_by_actor(self, audit, sample_admin, member_user):
        audit.log(sample_admin.id, "a")
        audit.log(member_user.id, "b")
        audit.log(sample_admin.id, "c")

        admin_events = audit.list_recent(actor_user_id=sample_admin.id)
        assert {e.action for e in admin_events} == {"a", "c"}

    def test_filter_by_action(self, audit, sample_admin):
        audit.log(sample_admin.id, "sudo_elevate")
        audit.log(sample_admin.id, "sudo_drop")
        audit.log(sample_admin.id, "sudo_elevate")

        events = audit.list_recent(action="sudo_elevate")
        assert len(events) == 2
        assert all(e.action == "sudo_elevate" for e in events)

    def test_filter_by_target(self, audit, sample_admin):
        audit.log(sample_admin.id, "stealth_read_note", target_type="note", target_id=12)
        audit.log(sample_admin.id, "stealth_read_note", target_type="note", target_id=99)
        audit.log(sample_admin.id, "stealth_read_wiki", target_type="wiki_page", target_id=12)

        events = audit.list_recent(target_type="note", target_id=12)
        assert len(events) == 1
        assert events[0].action == "stealth_read_note"

    def test_limit_and_offset_pagination(self, audit, sample_admin):
        ids = [audit.log(sample_admin.id, f"a_{i}") for i in range(5)]
        page1 = audit.list_recent(limit=2, offset=0)
        page2 = audit.list_recent(limit=2, offset=2)
        page3 = audit.list_recent(limit=2, offset=4)

        # Pages should not overlap and should be in DESC order.
        assert [e.id for e in page1] == [ids[4], ids[3]]
        assert [e.id for e in page2] == [ids[2], ids[1]]
        assert [e.id for e in page3] == [ids[0]]

    def test_limit_zero_returns_empty(self, audit, sample_admin):
        audit.log(sample_admin.id, "a")
        assert audit.list_recent(limit=0) == []

    def test_payload_none_roundtrips(self, audit, sample_admin):
        audit.log(sample_admin.id, "no_payload_action")
        events = audit.list_recent(limit=1)
        assert events[0].payload is None

    def test_payload_with_vietnamese_chars(self, audit, sample_admin):
        audit.log(
            sample_admin.id,
            "role_change",
            payload={"old": "member", "new": "manager", "note": "đổi role hằng ngày"},
        )
        events = audit.list_recent(limit=1)
        assert events[0].payload["note"] == "đổi role hằng ngày"

    def test_returns_audit_event_dataclass(self, audit, sample_admin):
        audit.log(sample_admin.id, "a")
        events = audit.list_recent(limit=1)
        assert isinstance(events[0], AuditEvent)
