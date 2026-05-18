"""Tests for acl.py — can_read() and filter_visible()."""
import pytest

import acl
from interfaces import User


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(user_id: int, role: str = "member") -> User:
    return User(id=user_id, name=f"User{user_id}", role=role)


# ── can_read ──────────────────────────────────────────────────────────────────

class TestCanRead:
    # scope='everyone' → readable by anyone regardless of role or ownership
    def test_everyone_scope_owner_admin(self):
        u = _user(1, "admin")
        assert acl.can_read(u, "everyone", owner_user_id=1) is True

    def test_everyone_scope_owner_member(self):
        u = _user(2, "member")
        assert acl.can_read(u, "everyone", owner_user_id=2) is True

    def test_everyone_scope_non_owner_admin(self):
        u = _user(1, "admin")
        assert acl.can_read(u, "everyone", owner_user_id=99) is True

    def test_everyone_scope_non_owner_member(self):
        u = _user(2, "member")
        assert acl.can_read(u, "everyone", owner_user_id=99) is True

    def test_everyone_scope_non_owner_manager(self):
        u = _user(3, "manager")
        assert acl.can_read(u, "everyone", owner_user_id=99) is True

    def test_everyone_scope_non_owner_readonly(self):
        u = _user(4, "readonly")
        assert acl.can_read(u, "everyone", owner_user_id=99) is True

    # scope='private' → only owner can read
    def test_private_scope_owner_admin(self):
        u = _user(1, "admin")
        assert acl.can_read(u, "private", owner_user_id=1) is True

    def test_private_scope_owner_member(self):
        u = _user(2, "member")
        assert acl.can_read(u, "private", owner_user_id=2) is True

    def test_private_scope_non_owner_admin(self):
        # FR-3: admin does NOT bypass private (stealth-read deferred to FR-4)
        u = _user(1, "admin")
        assert acl.can_read(u, "private", owner_user_id=99) is False

    def test_private_scope_non_owner_manager(self):
        u = _user(3, "manager")
        assert acl.can_read(u, "private", owner_user_id=99) is False

    def test_private_scope_non_owner_member(self):
        u = _user(2, "member")
        assert acl.can_read(u, "private", owner_user_id=99) is False

    def test_private_scope_non_owner_readonly(self):
        u = _user(4, "readonly")
        assert acl.can_read(u, "private", owner_user_id=99) is False

    # note_shares (reserved, FR-4+)
    def test_private_scope_explicit_share_grants_access(self):
        u = _user(5, "member")
        assert acl.can_read(u, "private", owner_user_id=99, note_shares=[5, 6]) is True

    def test_private_scope_not_in_shares(self):
        u = _user(7, "member")
        assert acl.can_read(u, "private", owner_user_id=99, note_shares=[5, 6]) is False

    def test_private_scope_empty_shares(self):
        u = _user(5, "member")
        assert acl.can_read(u, "private", owner_user_id=99, note_shares=[]) is False

    def test_private_scope_none_shares(self):
        u = _user(5, "member")
        assert acl.can_read(u, "private", owner_user_id=99, note_shares=None) is False


# ── filter_visible ────────────────────────────────────────────────────────────

class TestFilterVisible:
    def test_empty_list_returns_empty(self):
        u = _user(1)
        assert acl.filter_visible(u, []) == []

    def test_everyone_scope_passes_through(self):
        u = _user(1)
        rows = [{"drive_file_id": "f1", "scope": "everyone", "owner_user_id": 99}]
        result = acl.filter_visible(u, rows)
        assert len(result) == 1
        assert result[0]["drive_file_id"] == "f1"

    def test_private_owner_visible(self):
        u = _user(1)
        rows = [{"drive_file_id": "f1", "scope": "private", "owner_user_id": 1}]
        result = acl.filter_visible(u, rows)
        assert len(result) == 1

    def test_private_non_owner_invisible(self):
        u = _user(2)
        rows = [{"drive_file_id": "f1", "scope": "private", "owner_user_id": 1}]
        assert acl.filter_visible(u, rows) == []

    def test_mixed_rows_filtered_correctly(self):
        u = _user(1)
        rows = [
            {"drive_file_id": "pub",  "scope": "everyone", "owner_user_id": 99},
            {"drive_file_id": "mine", "scope": "private",  "owner_user_id": 1},
            {"drive_file_id": "theirs", "scope": "private", "owner_user_id": 2},
        ]
        result = acl.filter_visible(u, rows)
        ids = {r["drive_file_id"] for r in result}
        assert ids == {"pub", "mine"}

    def test_row_missing_scope_excluded(self):
        u = _user(1)
        rows = [{"drive_file_id": "f1", "owner_user_id": 1}]
        assert acl.filter_visible(u, rows) == []

    def test_row_missing_owner_excluded(self):
        u = _user(1)
        rows = [{"drive_file_id": "f1", "scope": "everyone"}]
        assert acl.filter_visible(u, rows) == []

    def test_all_private_non_owner_returns_empty(self):
        u = _user(5)
        rows = [
            {"drive_file_id": f"f{i}", "scope": "private", "owner_user_id": i}
            for i in range(1, 5)
        ]
        assert acl.filter_visible(u, rows) == []
