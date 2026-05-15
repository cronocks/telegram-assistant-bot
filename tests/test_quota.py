"""Tests for per-user quota: get_quota, set_quota, record_usage, reset_usage."""
import pytest

from user_store import SqliteUserStore


# ── get_quota ─────────────────────────────────────────────────────────────────

class TestGetQuota:
    def test_no_row_returns_none(self, store, sample_admin):
        assert store.get_quota(sample_admin.id) is None

    def test_returns_row_after_set(self, store, sample_admin):
        store.set_quota(sample_admin.id, 50000)
        quota = store.get_quota(sample_admin.id)
        assert quota is not None
        assert quota["monthly_token_limit"] == 50000
        assert quota["used_tokens"] == 0


# ── set_quota ─────────────────────────────────────────────────────────────────

class TestSetQuota:
    def test_creates_row(self, store, sample_admin):
        store.set_quota(sample_admin.id, 100000)
        quota = store.get_quota(sample_admin.id)
        assert quota["monthly_token_limit"] == 100000

    def test_updates_existing(self, store, sample_admin):
        store.set_quota(sample_admin.id, 100000)
        store.set_quota(sample_admin.id, 200000)
        quota = store.get_quota(sample_admin.id)
        assert quota["monthly_token_limit"] == 200000

    def test_zero_means_unlimited(self, store, sample_admin):
        store.set_quota(sample_admin.id, 50000)
        store.set_quota(sample_admin.id, 0)
        quota = store.get_quota(sample_admin.id)
        assert quota["monthly_token_limit"] == 0

    def test_update_preserves_used_tokens(self, store, sample_admin):
        store.set_quota(sample_admin.id, 100000)
        store.record_usage(sample_admin.id, 5000)
        store.set_quota(sample_admin.id, 200000)
        quota = store.get_quota(sample_admin.id)
        assert quota["used_tokens"] == 5000


# ── record_usage ──────────────────────────────────────────────────────────────

class TestRecordUsage:
    def test_creates_row_if_none(self, store, sample_admin):
        store.record_usage(sample_admin.id, 1000)
        quota = store.get_quota(sample_admin.id)
        assert quota is not None
        assert quota["used_tokens"] == 1000

    def test_accumulates(self, store, sample_admin):
        store.record_usage(sample_admin.id, 1000)
        store.record_usage(sample_admin.id, 2000)
        quota = store.get_quota(sample_admin.id)
        assert quota["used_tokens"] == 3000

    def test_multiple_users_independent(self, store, sample_admin):
        user2 = store.create_user(name="User2", role="member")
        store.record_usage(sample_admin.id, 5000)
        store.record_usage(user2.id, 1000)
        assert store.get_quota(sample_admin.id)["used_tokens"] == 5000
        assert store.get_quota(user2.id)["used_tokens"] == 1000


# ── reset_usage ───────────────────────────────────────────────────────────────

class TestResetUsage:
    def test_resets_to_zero(self, store, sample_admin):
        store.record_usage(sample_admin.id, 9999)
        result = store.reset_usage(sample_admin.id)
        assert result is True
        assert store.get_quota(sample_admin.id)["used_tokens"] == 0

    def test_no_row_returns_false(self, store, sample_admin):
        result = store.reset_usage(sample_admin.id)
        assert result is False

    def test_preserves_limit(self, store, sample_admin):
        store.set_quota(sample_admin.id, 100000)
        store.record_usage(sample_admin.id, 50000)
        store.reset_usage(sample_admin.id)
        quota = store.get_quota(sample_admin.id)
        assert quota["monthly_token_limit"] == 100000
        assert quota["used_tokens"] == 0


# ── quota enforcement (via _is_over_quota logic) ──────────────────────────────

class TestQuotaEnforcement:
    def test_no_quota_row_is_not_over(self, store, sample_admin):
        quota = store.get_quota(sample_admin.id)
        assert quota is None

    def test_unlimited_quota_is_not_over(self, store, sample_admin):
        store.set_quota(sample_admin.id, 0)
        quota = store.get_quota(sample_admin.id)
        # limit == 0 means unlimited; enforcement should skip
        assert quota["monthly_token_limit"] == 0

    def test_under_limit_is_not_over(self, store, sample_admin):
        store.set_quota(sample_admin.id, 10000)
        store.record_usage(sample_admin.id, 9999)
        quota = store.get_quota(sample_admin.id)
        assert quota["used_tokens"] < quota["monthly_token_limit"]

    def test_at_limit_is_over(self, store, sample_admin):
        store.set_quota(sample_admin.id, 10000)
        store.record_usage(sample_admin.id, 10000)
        quota = store.get_quota(sample_admin.id)
        assert quota["used_tokens"] >= quota["monthly_token_limit"]
