"""Tests for note_index.py — SqliteNoteIndex CRUD, scope, backfill, ACL."""
import pytest

from note_index import SqliteNoteIndex


# ── add_note ──────────────────────────────────────────────────────────────────

class TestAddNote:
    def test_returns_row_id(self, note_index, sample_admin):
        row_id = note_index.add_note("drive-001", sample_admin.id)
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_default_scope_is_private(self, note_index, sample_admin):
        note_index.add_note("drive-001", sample_admin.id)
        meta = note_index.get_note_meta("drive-001")
        assert meta["scope"] == "private"

    def test_default_kind_is_note(self, note_index, sample_admin):
        note_index.add_note("drive-001", sample_admin.id)
        meta = note_index.get_note_meta("drive-001")
        assert meta["kind"] == "note"

    def test_custom_kind_journal(self, note_index, sample_admin):
        note_index.add_note("drive-002", sample_admin.id, kind="journal")
        meta = note_index.get_note_meta("drive-002")
        assert meta["kind"] == "journal"

    def test_title_stored(self, note_index, sample_admin):
        note_index.add_note("drive-003", sample_admin.id, title="My Note")
        meta = note_index.get_note_meta("drive-003")
        assert meta["title"] == "My Note"

    def test_duplicate_drive_id_raises(self, note_index, sample_admin):
        note_index.add_note("drive-dup", sample_admin.id)
        with pytest.raises(Exception):
            note_index.add_note("drive-dup", sample_admin.id)


# ── add_wiki_page ─────────────────────────────────────────────────────────────

class TestAddWikiPage:
    def test_returns_row_id(self, note_index, sample_admin):
        row_id = note_index.add_wiki_page("wiki-001", sample_admin.id, "Topic A", "topic_a")
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_default_scope_is_everyone(self, note_index, sample_admin):
        note_index.add_wiki_page("wiki-001", sample_admin.id, "Topic A", "topic_a")
        meta = note_index.get_wiki_meta("wiki-001")
        assert meta["scope"] == "everyone"

    def test_topic_and_slug_stored(self, note_index, sample_admin):
        note_index.add_wiki_page("wiki-002", sample_admin.id, "Topic B", "topic_b")
        meta = note_index.get_wiki_meta("wiki-002")
        assert meta["topic"] == "Topic B"
        assert meta["slug"] == "topic_b"


# ── get_note_meta / get_wiki_meta ─────────────────────────────────────────────

class TestGetMeta:
    def test_missing_note_returns_none(self, note_index):
        assert note_index.get_note_meta("nonexistent") is None

    def test_missing_wiki_returns_none(self, note_index):
        assert note_index.get_wiki_meta("nonexistent") is None

    def test_note_meta_fields(self, note_index, sample_admin):
        note_index.add_note("drive-001", sample_admin.id, kind="note", title="T")
        meta = note_index.get_note_meta("drive-001")
        assert set(meta.keys()) == {"id", "drive_file_id", "owner_user_id", "scope", "kind", "title"}

    def test_wiki_meta_fields(self, note_index, sample_admin):
        note_index.add_wiki_page("wiki-001", sample_admin.id, "Topic", "topic")
        meta = note_index.get_wiki_meta("wiki-001")
        assert set(meta.keys()) == {"id", "drive_file_id", "owner_user_id", "scope", "topic", "slug"}


# ── touch_note / touch_wiki_page ──────────────────────────────────────────────

class TestTouch:
    def test_touch_note_does_not_raise(self, note_index, sample_admin):
        note_index.add_note("drive-001", sample_admin.id)
        note_index.touch_note("drive-001")  # should not raise

    def test_touch_wiki_does_not_raise(self, note_index, sample_admin):
        note_index.add_wiki_page("wiki-001", sample_admin.id, "T", "t")
        note_index.touch_wiki_page("wiki-001")

    def test_touch_nonexistent_is_silent(self, note_index):
        note_index.touch_note("ghost")
        note_index.touch_wiki_page("ghost")


# ── set_note_scope / set_wiki_scope ───────────────────────────────────────────

class TestSetScope:
    def test_owner_can_change_note_scope(self, note_index, sample_admin):
        note_index.add_note("drive-001", sample_admin.id)
        result = note_index.set_note_scope("drive-001", "everyone", sample_admin.id)
        assert result is True
        assert note_index.get_note_meta("drive-001")["scope"] == "everyone"

    def test_non_owner_cannot_change_note_scope(self, note_index, sample_admin, member_user):
        note_index.add_note("drive-001", sample_admin.id)
        result = note_index.set_note_scope("drive-001", "everyone", member_user.id)
        assert result is False
        assert note_index.get_note_meta("drive-001")["scope"] == "private"

    def test_owner_can_change_wiki_scope(self, note_index, sample_admin):
        note_index.add_wiki_page("wiki-001", sample_admin.id, "T", "t")
        result = note_index.set_wiki_scope("wiki-001", "private", sample_admin.id)
        assert result is True
        assert note_index.get_wiki_meta("wiki-001")["scope"] == "private"

    def test_non_owner_cannot_change_wiki_scope(self, note_index, sample_admin, member_user):
        note_index.add_wiki_page("wiki-001", sample_admin.id, "T", "t")
        result = note_index.set_wiki_scope("wiki-001", "private", member_user.id)
        assert result is False
        assert note_index.get_wiki_meta("wiki-001")["scope"] == "everyone"

    def test_set_scope_nonexistent_returns_false(self, note_index, sample_admin):
        assert note_index.set_note_scope("ghost", "everyone", sample_admin.id) is False
        assert note_index.set_wiki_scope("ghost", "private", sample_admin.id) is False


# ── note_meta_for_ids ─────────────────────────────────────────────────────────

class TestNoteMetaForIds:
    def test_empty_input_returns_empty(self, note_index):
        assert note_index.note_meta_for_ids([]) == []

    def test_returns_known_ids(self, note_index, sample_admin):
        note_index.add_note("f1", sample_admin.id)
        note_index.add_note("f2", sample_admin.id)
        result = note_index.note_meta_for_ids(["f1", "f2"])
        ids = {r["drive_file_id"] for r in result}
        assert ids == {"f1", "f2"}

    def test_orphan_id_omitted(self, note_index, sample_admin):
        note_index.add_note("f1", sample_admin.id)
        result = note_index.note_meta_for_ids(["f1", "orphan-id"])
        assert len(result) == 1
        assert result[0]["drive_file_id"] == "f1"

    def test_all_orphans_returns_empty(self, note_index):
        result = note_index.note_meta_for_ids(["ghost1", "ghost2"])
        assert result == []


# ── visible_wiki_slugs ────────────────────────────────────────────────────────

class TestVisibleWikiSlugs:
    def test_no_pages_returns_empty(self, note_index, sample_admin):
        slugs = note_index.visible_wiki_slugs(sample_admin.id)
        assert slugs == set()

    def test_everyone_scope_visible_to_all(self, note_index, sample_admin, member_user):
        note_index.add_wiki_page("w1", sample_admin.id, "Topic", "topic_a", scope="everyone")
        assert "topic_a" in note_index.visible_wiki_slugs(member_user.id)

    def test_private_scope_visible_to_owner_only(self, note_index, sample_admin, member_user):
        note_index.add_wiki_page("w1", sample_admin.id, "Secret", "secret", scope="private")
        assert "secret" in note_index.visible_wiki_slugs(sample_admin.id)
        assert "secret" not in note_index.visible_wiki_slugs(member_user.id)

    def test_mixed_pages(self, note_index, sample_admin, member_user):
        note_index.add_wiki_page("w1", sample_admin.id, "Public", "public_wiki", scope="everyone")
        note_index.add_wiki_page("w2", sample_admin.id, "Private", "private_wiki", scope="private")
        note_index.add_wiki_page("w3", member_user.id, "Mine", "mine_wiki", scope="private")

        admin_slugs = note_index.visible_wiki_slugs(sample_admin.id)
        member_slugs = note_index.visible_wiki_slugs(member_user.id)

        assert admin_slugs == {"public_wiki", "private_wiki"}
        assert member_slugs == {"public_wiki", "mine_wiki"}


# ── backfill ──────────────────────────────────────────────────────────────────

class TestBackfill:
    def test_inserts_new_note_files(self, note_index, sample_admin):
        files = [{"id": "f1", "name": "Note1.md"}, {"id": "f2", "name": "Note2.md"}]
        inserted = note_index.backfill(files, [], sample_admin.id)
        assert inserted == 2

    def test_inserts_new_wiki_files(self, note_index, sample_admin):
        files = [{"id": "w1", "name": "topic_a.md"}]
        inserted = note_index.backfill([], files, sample_admin.id)
        assert inserted == 1

    def test_idempotent_skips_existing(self, note_index, sample_admin):
        files = [{"id": "f1", "name": "Note1.md"}]
        first = note_index.backfill(files, [], sample_admin.id)
        second = note_index.backfill(files, [], sample_admin.id)
        assert first == 1
        assert second == 0

    def test_backfill_note_scope_is_private(self, note_index, sample_admin):
        note_index.backfill([{"id": "f1", "name": "Note1.md"}], [], sample_admin.id)
        meta = note_index.get_note_meta("f1")
        assert meta["scope"] == "private"
        assert meta["owner_user_id"] == sample_admin.id

    def test_backfill_wiki_scope_is_everyone(self, note_index, sample_admin):
        note_index.backfill([], [{"id": "w1", "name": "topic.md"}], sample_admin.id)
        meta = note_index.get_wiki_meta("w1")
        assert meta["scope"] == "everyone"

    def test_journal_filename_sets_kind_journal(self, note_index, sample_admin):
        note_index.backfill([{"id": "j1", "name": "2026-05-18_NhatKy.md"}], [], sample_admin.id)
        meta = note_index.get_note_meta("j1")
        assert meta["kind"] == "journal"

    def test_regular_filename_sets_kind_note(self, note_index, sample_admin):
        note_index.backfill([{"id": "n1", "name": "meeting_notes.md"}], [], sample_admin.id)
        meta = note_index.get_note_meta("n1")
        assert meta["kind"] == "note"

    def test_file_missing_id_skipped(self, note_index, sample_admin):
        files = [{"name": "no_id.md"}]
        inserted = note_index.backfill(files, [], sample_admin.id)
        assert inserted == 0

    def test_partial_existing_only_new_inserted(self, note_index, sample_admin):
        note_index.add_note("existing", sample_admin.id)
        files = [{"id": "existing", "name": "old.md"}, {"id": "new-one", "name": "new.md"}]
        inserted = note_index.backfill(files, [], sample_admin.id)
        assert inserted == 1
