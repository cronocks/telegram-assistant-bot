"""Tests for parent_links: set_parent, get_parent, get_children, remove_parent."""
import pytest

from user_store import SqliteUserStore


# ── set_parent ────────────────────────────────────────────────────────────────

class TestSetParent:
    def test_sets_parent(self, store, sample_admin):
        child = store.create_user(name="Child", role="member")
        store.set_parent(child.id, sample_admin.id, set_by=sample_admin.id)
        parent = store.get_parent(child.id)
        assert parent is not None
        assert parent.id == sample_admin.id

    def test_replaces_existing_parent(self, store, sample_admin):
        child = store.create_user(name="Child", role="member")
        old_parent = store.create_user(name="OldParent", role="member")
        new_parent = store.create_user(name="NewParent", role="member")
        store.set_parent(child.id, old_parent.id, set_by=sample_admin.id)
        store.set_parent(child.id, new_parent.id, set_by=sample_admin.id)
        parent = store.get_parent(child.id)
        assert parent.id == new_parent.id

    def test_self_parent_raises(self, store, sample_admin):
        with pytest.raises(ValueError, match="cannot be their own parent"):
            store.set_parent(sample_admin.id, sample_admin.id, set_by=sample_admin.id)

    def test_unknown_user_raises(self, store, sample_admin):
        with pytest.raises(ValueError, match="not found"):
            store.set_parent(9999, sample_admin.id, set_by=sample_admin.id)

    def test_unknown_parent_raises(self, store, sample_admin):
        with pytest.raises(ValueError, match="not found"):
            store.set_parent(sample_admin.id, 9999, set_by=sample_admin.id)


# ── get_parent ────────────────────────────────────────────────────────────────

class TestGetParent:
    def test_no_parent_returns_none(self, store, sample_admin):
        assert store.get_parent(sample_admin.id) is None

    def test_returns_correct_parent(self, store, sample_admin):
        child = store.create_user(name="Child", role="member")
        store.set_parent(child.id, sample_admin.id, set_by=sample_admin.id)
        result = store.get_parent(child.id)
        assert result.id == sample_admin.id
        assert result.name == "Admin User"


# ── get_children ──────────────────────────────────────────────────────────────

class TestGetChildren:
    def test_no_children_returns_empty(self, store, sample_admin):
        assert store.get_children(sample_admin.id) == []

    def test_returns_all_children(self, store, sample_admin):
        c1 = store.create_user(name="Child1", role="member")
        c2 = store.create_user(name="Child2", role="member")
        store.set_parent(c1.id, sample_admin.id, set_by=sample_admin.id)
        store.set_parent(c2.id, sample_admin.id, set_by=sample_admin.id)
        children = store.get_children(sample_admin.id)
        ids = {c.id for c in children}
        assert ids == {c1.id, c2.id}

    def test_replaced_parent_not_counted(self, store, sample_admin):
        child = store.create_user(name="Child", role="member")
        other = store.create_user(name="Other", role="member")
        store.set_parent(child.id, sample_admin.id, set_by=sample_admin.id)
        # Redirect child to a different parent
        store.set_parent(child.id, other.id, set_by=sample_admin.id)
        assert store.get_children(sample_admin.id) == []


# ── remove_parent ─────────────────────────────────────────────────────────────

class TestRemoveParent:
    def test_removes_active_link(self, store, sample_admin):
        child = store.create_user(name="Child", role="member")
        store.set_parent(child.id, sample_admin.id, set_by=sample_admin.id)
        result = store.remove_parent(child.id, removed_by=sample_admin.id)
        assert result is True
        assert store.get_parent(child.id) is None

    def test_no_active_link_returns_false(self, store, sample_admin):
        child = store.create_user(name="Child", role="member")
        result = store.remove_parent(child.id, removed_by=sample_admin.id)
        assert result is False

    def test_remove_idempotent(self, store, sample_admin):
        child = store.create_user(name="Child", role="member")
        store.set_parent(child.id, sample_admin.id, set_by=sample_admin.id)
        store.remove_parent(child.id, removed_by=sample_admin.id)
        result = store.remove_parent(child.id, removed_by=sample_admin.id)
        assert result is False

    def test_can_set_new_parent_after_removal(self, store, sample_admin):
        child = store.create_user(name="Child", role="member")
        other = store.create_user(name="Other", role="admin")
        store.set_parent(child.id, sample_admin.id, set_by=sample_admin.id)
        store.remove_parent(child.id, removed_by=sample_admin.id)
        store.set_parent(child.id, other.id, set_by=sample_admin.id)
        parent = store.get_parent(child.id)
        assert parent.id == other.id
