"""Tests for birthdate change flow — request, approve, reject."""
from datetime import date

import pytest

from user_store import SqliteUserStore


@pytest.fixture()
def manager(store):
    return store.create_user(name="Manager", role="manager")


@pytest.fixture()
def member(store):
    return store.create_user(name="Alice", role="member")


class TestRequestBirthdateChange:
    def test_creates_pending_request(self, store, member):
        bd = date(1995, 6, 15)
        req_id = store.request_birthdate_change(member.id, bd)
        assert isinstance(req_id, int)
        pending = store.get_pending_birthdate_change(member.id)
        assert pending is not None
        assert pending["new_birthdate"] == "1995-06-15"
        assert pending["approved_at"] is None
        assert pending["rejected_at"] is None

    def test_duplicate_pending_raises(self, store, member):
        store.request_birthdate_change(member.id, date(1995, 6, 15))
        with pytest.raises(ValueError, match="already has a pending"):
            store.request_birthdate_change(member.id, date(1996, 1, 1))

    def test_no_pending_returns_none(self, store, member):
        assert store.get_pending_birthdate_change(member.id) is None

    def test_can_request_again_after_rejection(self, store, member, sample_admin):
        req_id = store.request_birthdate_change(member.id, date(1995, 6, 15))
        store.reject_birthdate_change(req_id, sample_admin.id, "wrong date")
        # No pending request now
        assert store.get_pending_birthdate_change(member.id) is None
        # Can request again
        new_id = store.request_birthdate_change(member.id, date(1996, 3, 20))
        assert new_id != req_id


class TestApproveBirthdateChange:
    def test_approve_updates_user_birthdate(self, store, member, manager):
        bd = date(1992, 3, 10)
        req_id = store.request_birthdate_change(member.id, bd)
        result = store.approve_birthdate_change(req_id, manager.id)
        assert result is True

        updated = store.get_user_by_id(member.id)
        assert updated.birthdate == bd

    def test_approve_clears_pending(self, store, member, manager):
        req_id = store.request_birthdate_change(member.id, date(1992, 3, 10))
        store.approve_birthdate_change(req_id, manager.id)
        assert store.get_pending_birthdate_change(member.id) is None

    def test_approve_nonexistent_returns_false(self, store, manager):
        assert store.approve_birthdate_change(99999, manager.id) is False

    def test_approve_already_approved_returns_false(self, store, member, manager):
        req_id = store.request_birthdate_change(member.id, date(1992, 3, 10))
        store.approve_birthdate_change(req_id, manager.id)
        assert store.approve_birthdate_change(req_id, manager.id) is False


class TestRejectBirthdateChange:
    def test_reject_does_not_change_birthdate(self, store, member, manager):
        original_bd = member.birthdate  # None
        req_id = store.request_birthdate_change(member.id, date(1992, 3, 10))
        result = store.reject_birthdate_change(req_id, manager.id, "incorrect")
        assert result is True

        unchanged = store.get_user_by_id(member.id)
        assert unchanged.birthdate == original_bd

    def test_reject_clears_pending(self, store, member, manager):
        req_id = store.request_birthdate_change(member.id, date(1992, 3, 10))
        store.reject_birthdate_change(req_id, manager.id)
        assert store.get_pending_birthdate_change(member.id) is None

    def test_reject_nonexistent_returns_false(self, store, manager):
        assert store.reject_birthdate_change(99999, manager.id) is False

    def test_reject_already_rejected_returns_false(self, store, member, manager):
        req_id = store.request_birthdate_change(member.id, date(1992, 3, 10))
        store.reject_birthdate_change(req_id, manager.id, "wrong")
        assert store.reject_birthdate_change(req_id, manager.id) is False


class TestListPendingBirthdateChanges:
    def test_returns_all_pending(self, store, manager):
        u1 = store.create_user(name="User1", role="member")
        u2 = store.create_user(name="User2", role="member")
        store.request_birthdate_change(u1.id, date(1990, 1, 1))
        store.request_birthdate_change(u2.id, date(1991, 2, 2))

        pending = store.list_pending_birthdate_changes()
        assert len(pending) == 2

    def test_excludes_resolved(self, store, manager):
        u1 = store.create_user(name="User1", role="member")
        u2 = store.create_user(name="User2", role="member")
        r1 = store.request_birthdate_change(u1.id, date(1990, 1, 1))
        store.request_birthdate_change(u2.id, date(1991, 2, 2))
        store.approve_birthdate_change(r1, manager.id)

        pending = store.list_pending_birthdate_changes()
        assert len(pending) == 1
        assert pending[0]["user_name"] == "User2"
