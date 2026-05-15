"""Tests for SqliteUserStore — user CRUD + bootstrap (partial: no bindings yet)."""
from datetime import date
from unittest.mock import patch

import pytest

from user_store import SqliteUserStore


class TestCreateUser:
    def test_creates_with_required_fields(self, store):
        user = store.create_user(name="Alice", role="member")
        assert user.id is not None
        assert user.name == "Alice"
        assert user.role == "member"
        assert user.username is None
        assert user.birthdate is None
        assert user.is_active

    def test_creates_with_all_fields(self, store):
        bd = date(1990, 5, 15)
        user = store.create_user(name="Bob", role="manager", birthdate=bd, username="bob99")
        assert user.birthdate == bd
        assert user.username == "bob99"

    def test_rejects_invalid_role(self, store, db_conn):
        with pytest.raises(Exception):
            db_conn.execute(
                "INSERT INTO users (name, role) VALUES (?, ?)", ("X", "superuser")
            )
            db_conn.commit()

    def test_rejects_duplicate_username(self, store):
        store.create_user(name="Alice", role="member", username="alice")
        with pytest.raises(Exception):
            store.create_user(name="Alice2", role="member", username="alice")

    def test_username_case_insensitive_unique(self, store):
        store.create_user(name="Alice", role="member", username="Alice")
        with pytest.raises(Exception):
            store.create_user(name="Alice2", role="member", username="alice")


class TestGetUser:
    def test_get_existing(self, store, sample_admin):
        fetched = store.get_user_by_id(sample_admin.id)
        assert fetched is not None
        assert fetched.id == sample_admin.id
        assert fetched.name == sample_admin.name

    def test_get_nonexistent_returns_none(self, store):
        assert store.get_user_by_id(99999) is None


class TestListUsers:
    def test_lists_active_only_by_default(self, store):
        u1 = store.create_user(name="Alice", role="member")
        u2 = store.create_user(name="Bob", role="member")
        store.soft_delete_user(u2.id)

        active = store.list_users()
        ids = [u.id for u in active]
        assert u1.id in ids
        assert u2.id not in ids

    def test_include_deleted(self, store):
        u1 = store.create_user(name="Alice", role="member")
        u2 = store.create_user(name="Bob", role="member")
        store.soft_delete_user(u2.id)

        all_users = store.list_users(include_deleted=True)
        ids = [u.id for u in all_users]
        assert u1.id in ids
        assert u2.id in ids


class TestSoftDelete:
    def test_soft_delete_sets_deleted_at(self, store, sample_admin):
        store.soft_delete_user(sample_admin.id)
        user = store.get_user_by_id(sample_admin.id)
        assert user is not None
        assert not user.is_active
        assert user.deleted_at is not None

    def test_soft_delete_idempotent(self, store, sample_admin):
        store.soft_delete_user(sample_admin.id)
        store.soft_delete_user(sample_admin.id)  # second call must not raise
        user = store.get_user_by_id(sample_admin.id)
        assert not user.is_active


class TestUpdateRole:
    def test_update_role(self, store, sample_admin):
        store.update_user_role(sample_admin.id, "readonly")
        user = store.get_user_by_id(sample_admin.id)
        assert user.role == "readonly"

    def test_update_role_invalid_raises(self, store, sample_admin, db_conn):
        with pytest.raises(Exception):
            db_conn.execute(
                "UPDATE users SET role = ? WHERE id = ?", ("superuser", sample_admin.id)
            )
            db_conn.commit()


class TestBootstrapAdmin:
    def test_creates_admin_when_empty(self, store):
        with patch("config.TELEGRAM_CHAT_ID", "123456"):
            admin = store.bootstrap_admin()
        assert admin is not None
        assert admin.role == "admin"
        assert admin.name == "Bot Owner"

    def test_skips_when_users_exist(self, store, sample_admin):
        with patch("config.TELEGRAM_CHAT_ID", "123456"):
            result = store.bootstrap_admin()
        # Returns first existing user, does not create a second
        assert store.list_users().__len__() == 1
        assert result.id == sample_admin.id

    def test_returns_none_without_chat_id(self, store):
        with patch("config.TELEGRAM_CHAT_ID", None):
            result = store.bootstrap_admin()
        assert result is None
        assert store.list_users() == []

    def test_idempotent_on_repeated_calls(self, store):
        with patch("config.TELEGRAM_CHAT_ID", "123456"):
            store.bootstrap_admin()
            store.bootstrap_admin()
        assert len(store.list_users()) == 1

    def test_bootstrap_binds_telegram_channel(self, store):
        with patch("config.TELEGRAM_CHAT_ID", "123456"):
            admin = store.bootstrap_admin()
        found = store.find_by_channel("telegram", "123456")
        assert found is not None
        assert found.id == admin.id


class TestChannelBindings:
    def test_bind_and_find(self, store, sample_admin):
        store.bind_channel(sample_admin.id, "telegram", "111")
        user = store.find_by_channel("telegram", "111")
        assert user is not None
        assert user.id == sample_admin.id

    def test_find_unknown_returns_none(self, store):
        assert store.find_by_channel("telegram", "999") is None

    def test_duplicate_binding_raises(self, store, sample_admin):
        store.bind_channel(sample_admin.id, "telegram", "111")
        with pytest.raises(Exception):
            store.bind_channel(sample_admin.id, "telegram", "111")

    def test_different_channels_are_independent(self, store, sample_admin):
        store.bind_channel(sample_admin.id, "telegram", "111")
        store.bind_channel(sample_admin.id, "discord", "111")
        assert store.find_by_channel("telegram", "111") is not None
        assert store.find_by_channel("discord", "111") is not None


class TestInviteCodes:
    def test_create_and_consume(self, store, sample_admin):
        member = store.create_user(name="Alice", role="member")
        code = store.create_invite_code(
            intended_user_id=member.id, created_by=sample_admin.id
        )
        assert len(code) == 8

        user = store.consume_invite_code(code, "telegram", "222")
        assert user is not None
        assert user.id == member.id
        # Binding created
        assert store.find_by_channel("telegram", "222") is not None

    def test_consumed_code_cannot_be_reused(self, store, sample_admin):
        member = store.create_user(name="Alice", role="member")
        code = store.create_invite_code(
            intended_user_id=member.id, created_by=sample_admin.id
        )
        store.consume_invite_code(code, "telegram", "222")
        result = store.consume_invite_code(code, "telegram", "333")
        assert result is None

    def test_invalid_code_returns_none(self, store):
        assert store.consume_invite_code("badcode1", "telegram", "444") is None
