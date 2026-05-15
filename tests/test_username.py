"""Tests for username validation, set/change flow, rate-limit, and approval."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from text_utils import validate_username
from user_store import SqliteUserStore


# ── validate_username ─────────────────────────────────────────────────────────

class TestValidateUsername:
    def test_valid_simple(self):
        assert validate_username("alice") is None

    def test_valid_with_allowed_chars(self):
        assert validate_username("alice.99_ok-name") is None

    def test_too_short(self):
        assert validate_username("ab") is not None

    def test_too_long(self):
        assert validate_username("a" * 33) is not None

    def test_exactly_3_chars(self):
        assert validate_username("abc") is None

    def test_exactly_32_chars(self):
        assert validate_username("a" * 32) is None

    def test_invalid_char_space(self):
        assert validate_username("alice bob") is not None

    def test_invalid_char_at_sign(self):
        assert validate_username("alice@99") is not None

    def test_reserved_lowercase(self):
        assert validate_username("admin") is not None

    def test_reserved_uppercase(self):
        assert validate_username("Admin") is not None

    def test_reserved_mixed_case(self):
        assert validate_username("ADMIN") is not None

    def test_all_reserved_names(self):
        reserved = ["admin", "root", "bot", "system", "support",
                    "owner", "null", "undefined", "me", "you"]
        for name in reserved:
            assert validate_username(name) is not None, f"'{name}' should be reserved"


# ── set_username_direct (first-set) ───────────────────────────────────────────

class TestSetUsernameDirect:
    def test_sets_when_null(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "owner99")
        user = store.get_user_by_id(sample_admin.id)
        assert user.username == "owner99"

    def test_raises_when_already_set(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "owner99")
        with pytest.raises(ValueError, match="already has username"):
            store.set_username_direct(sample_admin.id, "other")

    def test_case_insensitive_uniqueness(self, store):
        u1 = store.create_user(name="Alice", role="member")
        u2 = store.create_user(name="Bob", role="member")
        store.set_username_direct(u1.id, "Alice")
        with pytest.raises(Exception):
            store.set_username_direct(u2.id, "alice")


# ── request_username_change ───────────────────────────────────────────────────

class TestRequestUsernameChange:
    def test_creates_pending_request(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "owner99")
        req_id = store.request_username_change(sample_admin.id, "owner100")
        assert isinstance(req_id, int)
        pending = store.get_pending_username_change(sample_admin.id)
        assert pending is not None
        assert pending["new_username"] == "owner100"
        assert pending["old_username"] == "owner99"

    def test_duplicate_pending_raises(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "owner99")
        store.request_username_change(sample_admin.id, "owner100")
        with pytest.raises(ValueError, match="already has a pending"):
            store.request_username_change(sample_admin.id, "owner101")

    def test_rate_limit_blocks_within_30_days(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "first")
        req_id = store.request_username_change(sample_admin.id, "second")
        # Approve to record approved_at
        store.approve_username_change(req_id, sample_admin.id)
        # Try again immediately — should be blocked
        with pytest.raises(ValueError, match="Phải chờ"):
            store.request_username_change(sample_admin.id, "third")

    def test_no_pending_returns_none(self, store, sample_admin):
        assert store.get_pending_username_change(sample_admin.id) is None


# ── approve_username_change ───────────────────────────────────────────────────

class TestApproveUsernameChange:
    def test_approve_updates_username(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "old_name")
        req_id = store.request_username_change(sample_admin.id, "new_name")
        result = store.approve_username_change(req_id, sample_admin.id)
        assert result is True
        user = store.get_user_by_id(sample_admin.id)
        assert user.username == "new_name"

    def test_approve_clears_pending(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "old_name")
        req_id = store.request_username_change(sample_admin.id, "new_name")
        store.approve_username_change(req_id, sample_admin.id)
        assert store.get_pending_username_change(sample_admin.id) is None

    def test_approve_nonexistent_returns_false(self, store, sample_admin):
        assert store.approve_username_change(99999, sample_admin.id) is False

    def test_approve_twice_returns_false(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "old_name")
        req_id = store.request_username_change(sample_admin.id, "new_name")
        store.approve_username_change(req_id, sample_admin.id)
        assert store.approve_username_change(req_id, sample_admin.id) is False


# ── reject_username_change ────────────────────────────────────────────────────

class TestRejectUsernameChange:
    def test_reject_does_not_change_username(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "old_name")
        req_id = store.request_username_change(sample_admin.id, "new_name")
        result = store.reject_username_change(req_id, sample_admin.id, "not allowed")
        assert result is True
        user = store.get_user_by_id(sample_admin.id)
        assert user.username == "old_name"

    def test_reject_clears_pending(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "old_name")
        req_id = store.request_username_change(sample_admin.id, "new_name")
        store.reject_username_change(req_id, sample_admin.id)
        assert store.get_pending_username_change(sample_admin.id) is None

    def test_can_request_again_after_rejection(self, store, sample_admin):
        store.set_username_direct(sample_admin.id, "old_name")
        req_id = store.request_username_change(sample_admin.id, "new_name")
        store.reject_username_change(req_id, sample_admin.id)
        # After rejection, no rate-limit applies (no approved_at set)
        new_id = store.request_username_change(sample_admin.id, "another_name")
        assert new_id != req_id

    def test_reject_nonexistent_returns_false(self, store, sample_admin):
        assert store.reject_username_change(99999, sample_admin.id) is False
