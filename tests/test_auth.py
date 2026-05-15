"""Tests for auth.py (hash/verify) and UserStore password methods."""
import pytest

from auth import hash_password, verify_password, needs_rehash
from user_store import SqliteUserStore


# ── hash_password / verify_password ──────────────────────────────────────────

class TestHashVerify:
    def test_hash_is_not_plaintext(self):
        h = hash_password("secret")
        assert h != "secret"
        assert h.startswith("$argon2")

    def test_correct_password_verifies(self):
        h = hash_password("correct")
        assert verify_password("correct", h) is True

    def test_wrong_password_fails(self):
        h = hash_password("correct")
        assert verify_password("wrong", h) is False

    def test_empty_password_verifies(self):
        h = hash_password("")
        assert verify_password("", h) is True
        assert verify_password("x", h) is False

    def test_two_hashes_of_same_password_differ(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2

    def test_verify_invalid_hash_returns_false(self):
        assert verify_password("anything", "not-a-valid-hash") is False

    def test_needs_rehash_on_fresh_hash_is_false(self):
        h = hash_password("test")
        assert needs_rehash(h) is False


# ── UserStore.set_password / check_password ───────────────────────────────────

class TestUserStorePassword:
    def test_set_and_check_correct(self, store, sample_admin):
        store.set_password(sample_admin.id, "mypassword")
        assert store.check_password(sample_admin.id, "mypassword") is True

    def test_wrong_password_fails(self, store, sample_admin):
        store.set_password(sample_admin.id, "mypassword")
        assert store.check_password(sample_admin.id, "wrong") is False

    def test_no_password_set_returns_false(self, store, sample_admin):
        assert store.check_password(sample_admin.id, "anything") is False

    def test_unknown_user_returns_false(self, store):
        assert store.check_password(9999, "anything") is False

    def test_set_password_unknown_user_raises(self, store):
        with pytest.raises(ValueError, match="not found"):
            store.set_password(9999, "pass")

    def test_overwrite_password(self, store, sample_admin):
        store.set_password(sample_admin.id, "first")
        store.set_password(sample_admin.id, "second")
        assert store.check_password(sample_admin.id, "first") is False
        assert store.check_password(sample_admin.id, "second") is True
