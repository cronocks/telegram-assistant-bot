"""Tests for FR-4 sub-4.3 — recycle bin store-level methods.

Covers UserStore.{list_deleted,restore,hard_delete}_user and
NoteIndex.{soft_delete,list_deleted,restore,hard_delete}_{note,wiki}.

Drive deletion is exercised in tests/test_recycle_bin_handlers.py via fakes.
"""
from __future__ import annotations

from datetime import date

import pytest

from note_index import SqliteNoteIndex


@pytest.fixture()
def idx(db_conn):
    return SqliteNoteIndex(conn=db_conn)


# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────


class TestUserRecycleBin:

    def test_list_deleted_users_empty(self, store):
        assert store.list_deleted_users() == []

    def test_list_deleted_users_returns_only_deleted(self, store, sample_admin, member_user):
        store.soft_delete_user(member_user.id)
        deleted = store.list_deleted_users()
        assert len(deleted) == 1
        assert deleted[0].id == member_user.id
        # sample_admin still active → not in list.
        assert sample_admin.id not in {u.id for u in deleted}

    def test_list_deleted_users_orders_by_deleted_at_desc(self, store):
        a = store.create_user(name="A", role="member")
        b = store.create_user(name="B", role="member")
        # Delete b first then a; expected order: a (newer), b (older).
        store.soft_delete_user(b.id)
        # Force a measurable gap if needed; sqlite CURRENT_TIMESTAMP has 1s
        # resolution so we manually bump deleted_at on a.
        store.soft_delete_user(a.id)
        store._conn.execute(
            "UPDATE users SET deleted_at = '2099-01-01 00:00:00' WHERE id = ?", (a.id,),
        )
        deleted = store.list_deleted_users()
        assert [u.id for u in deleted] == [a.id, b.id]

    def test_restore_user_clears_deleted_at(self, store, member_user):
        store.soft_delete_user(member_user.id)
        ok = store.restore_user(member_user.id)
        assert ok is True
        restored = store.get_user_by_id(member_user.id)
        assert restored.is_active
        assert restored.deleted_at is None

    def test_restore_user_returns_false_when_not_deleted(self, store, member_user):
        # Was never soft-deleted.
        assert store.restore_user(member_user.id) is False

    def test_restore_user_returns_false_when_unknown(self, store):
        assert store.restore_user(99999) is False

    def test_hard_delete_user_fails_on_fk_constraint(self, store, sample_admin, member_user):
        """Active references (channel_bindings, parent_links...) should block hard delete."""
        store.bind_channel(member_user.id, "telegram", "999")
        store.soft_delete_user(member_user.id)
        # FK to channel_bindings is still alive.
        assert store.hard_delete_user(member_user.id) is False
        # User row still exists (just soft-deleted).
        assert store.get_user_by_id(member_user.id) is not None

    def test_hard_delete_user_succeeds_when_no_references(self, store):
        """A user with no FK referrers can be hard-deleted."""
        u = store.create_user(name="Lone", role="readonly")
        store.soft_delete_user(u.id)
        assert store.hard_delete_user(u.id) is True
        assert store.get_user_by_id(u.id) is None

    def test_hard_delete_user_unknown_returns_false(self, store):
        assert store.hard_delete_user(99999) is False


# ─────────────────────────────────────────────────────────────────────────────
# Notes
# ─────────────────────────────────────────────────────────────────────────────


class TestNoteRecycleBin:

    def test_soft_delete_note_sets_deleted_at(self, idx, sample_admin):
        note_id = idx.add_note("file-1", sample_admin.id, title="N1")
        assert idx.soft_delete_note(note_id) is True
        # get_note_meta filters deleted_at IS NULL → returns None.
        assert idx.get_note_meta("file-1") is None

    def test_soft_delete_note_idempotent(self, idx, sample_admin):
        note_id = idx.add_note("file-1", sample_admin.id)
        idx.soft_delete_note(note_id)
        # Second call: row already deleted_at → returns False.
        assert idx.soft_delete_note(note_id) is False

    def test_soft_delete_note_unknown_returns_false(self, idx):
        assert idx.soft_delete_note(99999) is False

    def test_list_deleted_notes_includes_only_deleted(self, idx, sample_admin):
        idx.add_note("alive", sample_admin.id, title="Alive")
        nid = idx.add_note("dead", sample_admin.id, title="Dead")
        idx.soft_delete_note(nid)
        deleted = idx.list_deleted_notes()
        assert len(deleted) == 1
        assert deleted[0]["drive_file_id"] == "dead"

    def test_restore_note_clears_deleted_at(self, idx, sample_admin):
        nid = idx.add_note("file-1", sample_admin.id, title="N1")
        idx.soft_delete_note(nid)
        assert idx.restore_note(nid) is True
        # Now visible to ACL again.
        assert idx.get_note_meta("file-1") is not None

    def test_restore_note_returns_false_when_not_deleted(self, idx, sample_admin):
        nid = idx.add_note("file-1", sample_admin.id)
        assert idx.restore_note(nid) is False  # never soft-deleted

    def test_hard_delete_note_returns_meta_and_removes_row(self, idx, sample_admin):
        nid = idx.add_note("drive_abc", sample_admin.id, title="Doomed", scope="private")
        meta = idx.hard_delete_note(nid)
        assert meta is not None
        assert meta["drive_file_id"] == "drive_abc"
        assert meta["owner_user_id"] == sample_admin.id
        # Row gone from DB entirely.
        assert idx.list_deleted_notes() == []
        assert idx.get_note_meta("drive_abc") is None

    def test_hard_delete_note_unknown_returns_none(self, idx):
        assert idx.hard_delete_note(99999) is None


# ─────────────────────────────────────────────────────────────────────────────
# Wiki pages
# ─────────────────────────────────────────────────────────────────────────────


class TestWikiRecycleBin:

    def test_soft_delete_wiki_sets_deleted_at(self, idx, sample_admin):
        wid = idx.add_wiki_page("w-1", sample_admin.id, topic="Topic", slug="topic")
        assert idx.soft_delete_wiki(wid) is True
        assert idx.get_wiki_meta("w-1") is None

    def test_soft_delete_wiki_idempotent(self, idx, sample_admin):
        wid = idx.add_wiki_page("w-1", sample_admin.id, topic="T", slug="t")
        idx.soft_delete_wiki(wid)
        assert idx.soft_delete_wiki(wid) is False

    def test_soft_delete_wiki_unknown_returns_false(self, idx):
        assert idx.soft_delete_wiki(99999) is False

    def test_list_deleted_wiki_pages_filters_correctly(self, idx, sample_admin):
        idx.add_wiki_page("alive", sample_admin.id, topic="Alive", slug="alive")
        wid = idx.add_wiki_page("dead", sample_admin.id, topic="Dead", slug="dead")
        idx.soft_delete_wiki(wid)
        deleted = idx.list_deleted_wiki_pages()
        assert {w["drive_file_id"] for w in deleted} == {"dead"}

    def test_restore_wiki_clears_deleted_at(self, idx, sample_admin):
        wid = idx.add_wiki_page("w-1", sample_admin.id, topic="T", slug="t")
        idx.soft_delete_wiki(wid)
        assert idx.restore_wiki(wid) is True
        assert idx.get_wiki_meta("w-1") is not None

    def test_hard_delete_wiki_returns_meta(self, idx, sample_admin):
        wid = idx.add_wiki_page("drive_w", sample_admin.id, topic="X", slug="x")
        meta = idx.hard_delete_wiki(wid)
        assert meta is not None
        assert meta["drive_file_id"] == "drive_w"
        assert meta["topic"] == "X"
        assert idx.list_deleted_wiki_pages() == []

    def test_hard_delete_wiki_unknown_returns_none(self, idx):
        assert idx.hard_delete_wiki(99999) is None

    def test_hard_delete_wiki_skips_drive_file_id_lookup_when_already_purged(
        self, idx, sample_admin,
    ):
        wid = idx.add_wiki_page("w-1", sample_admin.id, topic="T", slug="t")
        idx.hard_delete_wiki(wid)
        # Second call: row gone → returns None.
        assert idx.hard_delete_wiki(wid) is None


# ─────────────────────────────────────────────────────────────────────────────
# Cross-table integrity
# ─────────────────────────────────────────────────────────────────────────────


class TestRecycleBinIntegrity:

    def test_restore_then_modify_note_works(self, idx, sample_admin):
        """After restore, normal write paths (touch, scope change) should work."""
        nid = idx.add_note("file-1", sample_admin.id, title="N", scope="private")
        idx.soft_delete_note(nid)
        idx.restore_note(nid)
        # set_note_scope filters on deleted_at IS NULL — should work.
        assert idx.set_note_scope("file-1", "everyone", sample_admin.id) is True

    def test_hard_deleted_note_cannot_be_restored(self, idx, sample_admin):
        nid = idx.add_note("file-1", sample_admin.id)
        idx.hard_delete_note(nid)
        # Row gone → restore returns False (nothing to update).
        assert idx.restore_note(nid) is False

    def test_soft_delete_does_not_affect_other_rows(self, idx, sample_admin):
        a = idx.add_note("a", sample_admin.id, title="A")
        b = idx.add_note("b", sample_admin.id, title="B")
        idx.soft_delete_note(a)
        # b unaffected.
        assert idx.get_note_meta("b") is not None
