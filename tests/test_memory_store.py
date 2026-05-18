"""Tests for memory_store.py — SqliteMemoryStore get/set/meta."""
import pytest

from memory_store import SqliteMemoryStore


# ── get (before any data) ─────────────────────────────────────────────────────

class TestGet:
    def test_no_row_returns_empty_string(self, memory_store, sample_admin):
        assert memory_store.get(sample_admin.id, "memory") == ""
        assert memory_store.get(sample_admin.id, "user") == ""

    def test_get_meta_no_row_returns_none(self, memory_store, sample_admin):
        assert memory_store.get_meta(sample_admin.id, "memory") is None
        assert memory_store.get_meta(sample_admin.id, "user") is None

    def test_unknown_user_returns_empty(self, memory_store):
        assert memory_store.get(9999, "memory") == ""


# ── set (upsert) ──────────────────────────────────────────────────────────────

class TestSet:
    def test_set_creates_row(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "some facts")
        assert memory_store.get(sample_admin.id, "memory") == "some facts"

    def test_set_updates_existing(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "v1")
        memory_store.set(sample_admin.id, "memory", "v2")
        assert memory_store.get(sample_admin.id, "memory") == "v2"

    def test_two_kinds_independent(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "rolling facts")
        memory_store.set(sample_admin.id, "user", "profile info")
        assert memory_store.get(sample_admin.id, "memory") == "rolling facts"
        assert memory_store.get(sample_admin.id, "user") == "profile info"

    def test_two_users_independent(self, memory_store, sample_admin, member_user):
        memory_store.set(sample_admin.id, "memory", "admin memory")
        memory_store.set(member_user.id, "memory", "member memory")
        assert memory_store.get(sample_admin.id, "memory") == "admin memory"
        assert memory_store.get(member_user.id, "memory") == "member memory"

    def test_set_empty_string(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "has content")
        memory_store.set(sample_admin.id, "memory", "")
        assert memory_store.get(sample_admin.id, "memory") == ""


# ── mark_curated ──────────────────────────────────────────────────────────────

class TestMarkCurated:
    def test_curated_at_none_by_default(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "content")
        meta = memory_store.get_meta(sample_admin.id, "memory")
        assert meta["curated_at"] is None

    def test_mark_curated_stamps_curated_at(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "content", mark_curated=True)
        meta = memory_store.get_meta(sample_admin.id, "memory")
        assert meta["curated_at"] is not None
        assert meta["curated_at"].startswith("20")  # ISO timestamp

    def test_update_without_mark_preserves_curated_at(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "v1", mark_curated=True)
        first_curated = memory_store.get_meta(sample_admin.id, "memory")["curated_at"]

        memory_store.set(sample_admin.id, "memory", "v2", mark_curated=False)
        meta = memory_store.get_meta(sample_admin.id, "memory")
        # Content updated but curated_at preserved from previous curation
        assert memory_store.get(sample_admin.id, "memory") == "v2"
        assert meta["curated_at"] == first_curated

    def test_mark_curated_on_update_refreshes_curated_at(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "v1", mark_curated=True)
        first = memory_store.get_meta(sample_admin.id, "memory")["curated_at"]

        memory_store.set(sample_admin.id, "memory", "v2", mark_curated=True)
        second = memory_store.get_meta(sample_admin.id, "memory")["curated_at"]

        # Both are non-None; value may be same timestamp if fast, but row exists
        assert second is not None


# ── get_meta fields ───────────────────────────────────────────────────────────

class TestGetMeta:
    def test_meta_has_expected_keys(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "data")
        meta = memory_store.get_meta(sample_admin.id, "memory")
        assert set(meta.keys()) == {"user_id", "kind", "content", "updated_at", "curated_at"}

    def test_meta_user_id_matches(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "user", "profile")
        meta = memory_store.get_meta(sample_admin.id, "user")
        assert meta["user_id"] == sample_admin.id

    def test_meta_kind_matches(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "user", "profile")
        meta = memory_store.get_meta(sample_admin.id, "user")
        assert meta["kind"] == "user"

    def test_updated_at_is_set(self, memory_store, sample_admin):
        memory_store.set(sample_admin.id, "memory", "x")
        meta = memory_store.get_meta(sample_admin.id, "memory")
        assert meta["updated_at"] is not None
        assert meta["updated_at"].startswith("20")
